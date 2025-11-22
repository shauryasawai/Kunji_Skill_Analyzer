import json
import re
from openai import OpenAI
import pandas as pd
from pathlib import Path
from django.conf import settings
from PyPDF2 import PdfReader
from docx import Document
import os
import requests
from fuzzywuzzy import fuzz
from collections import defaultdict, Counter
import numpy as np

# ============================================================================
# SKILL SYNONYMS AND VARIATIONS
# ============================================================================

SKILL_SYNONYMS = {
    'javascript': ['js', 'ecmascript', 'node.js', 'nodejs', 'node'],
    'typescript': ['ts'],
    'python': ['py'],
    'aws': ['amazon web services', 'amazon aws'],
    'gcp': ['google cloud platform', 'google cloud'],
    'azure': ['microsoft azure', 'ms azure'],
    'react': ['reactjs', 'react.js'],
    'angular': ['angularjs', 'angular.js'],
    'vue': ['vuejs', 'vue.js'],
    'postgresql': ['postgres', 'psql'],
    'mongodb': ['mongo'],
    'mysql': ['my sql'],
    'rest api': ['restful api', 'rest', 'restful', 'rest apis'],
    'graphql': ['graph ql'],
    'docker': ['containerization'],
    'kubernetes': ['k8s'],
    'machine learning': ['ml'],
    'artificial intelligence': ['ai'],
    'natural language processing': ['nlp'],
    'ci/cd': ['continuous integration', 'continuous deployment', 'cicd'],
    'agile': ['scrum', 'kanban'],
    'sql': ['structured query language'],
    'nosql': ['no sql'],
    'html': ['html5'],
    'css': ['css3'],
}

# Build reverse lookup
SKILL_CANONICAL = {}
for canonical, synonyms in SKILL_SYNONYMS.items():
    SKILL_CANONICAL[canonical] = canonical
    for syn in synonyms:
        SKILL_CANONICAL[syn] = canonical

# ============================================================================
# SKILL NORMALIZATION AND MATCHING
# ============================================================================

def normalize_skill(skill):
    '''Normalize skill to canonical form'''
    skill = skill.lower().strip()
    # Remove special characters but keep hyphens and dots for technical terms
    skill = re.sub(r'[^\w\s\.\-\+\#]', ' ', skill)
    skill = ' '.join(skill.split())  # Normalize whitespace
    
    # Check if it's a known synonym
    if skill in SKILL_CANONICAL:
        return SKILL_CANONICAL[skill]
    
    return skill

def extract_skill_versions(skill):
    '''Extract version numbers from skills (e.g., Python 3.x, Java 11)'''
    # Match version patterns - handles "3.x", "3.11", "11", etc.
    version_pattern = r'(\d+(?:\.\d+)*(?:\.[xX])?)'
    match = re.search(version_pattern, skill)
    
    if match:
        base_skill = re.sub(version_pattern, '', skill).strip()
        version = match.group(1)
        return base_skill, version
    
    return skill, None

def calculate_skill_similarity(req_skill, cand_skill, use_fuzzy=True):
    '''
    Calculate similarity score between two skills (0-100)
    
    Returns: (score, match_type, details)
    '''
    # Normalize both skills
    req_norm = normalize_skill(req_skill)
    cand_norm = normalize_skill(cand_skill)
    
    # Extract versions if present
    req_base, req_version = extract_skill_versions(req_norm)
    cand_base, cand_version = extract_skill_versions(cand_norm)
    
    # 1. Exact match (canonical forms)
    if req_norm == cand_norm:
        return 100, "exact", {"matched": "canonical exact match"}
    
    # 2. Base skill match with version consideration
    if req_base == cand_base:
        if req_version and cand_version:
            # Both have versions - minor version mismatch acceptable
            if req_version.split('.')[0] == cand_version.split('.')[0]:
                return 95, "version_match", {"req_version": req_version, "cand_version": cand_version}
            else:
                return 85, "version_mismatch", {"req_version": req_version, "cand_version": cand_version}
        return 100, "exact", {"matched": "base skill exact"}
    
    # 3. One contains the other (substring)
    if req_norm in cand_norm:
        return 80, "substring", {"direction": "req in cand"}
    if cand_norm in req_norm:
        return 80, "substring", {"direction": "cand in req"}
    
    # 4. Fuzzy matching
    if use_fuzzy:
        # Token sort ratio (good for reordered words)
        token_sort = fuzz.token_sort_ratio(req_norm, cand_norm)
        # Partial ratio (good for substring matches)
        partial = fuzz.partial_ratio(req_norm, cand_norm)
        # Token set ratio (good for ignoring duplicates)
        token_set = fuzz.token_set_ratio(req_norm, cand_norm)
        
        # Use the best score
        best_fuzzy = max(token_sort, partial, token_set)
        
        if best_fuzzy >= 85:
            score = 50 + ((best_fuzzy - 85) * 2)  # 85-100 maps to 50-80
            return score, "fuzzy_high", {"similarity": best_fuzzy}
        elif best_fuzzy >= 75:
            score = 40 + ((best_fuzzy - 75) * 1)  # 75-85 maps to 40-50
            return score, "fuzzy_medium", {"similarity": best_fuzzy}
    
    # 5. Word overlap (intelligent)
    req_words = set(req_norm.split())
    cand_words = set(cand_norm.split())
    
    # Remove stopwords
    stopwords = {'and', 'or', 'the', 'a', 'an', 'of', 'in', 'to', 'for', 'with', 'on', 'at', 'by'}
    req_words_clean = req_words - stopwords
    cand_words_clean = cand_words - stopwords
    
    if req_words_clean and cand_words_clean:
        common = req_words_clean & cand_words_clean
        
        if common:
            # Calculate Jaccard similarity
            union = req_words_clean | cand_words_clean
            jaccard = len(common) / len(union)
            
            # Weight by importance of matched words
            if len(common) >= 2:  # Multiple word match
                score = 45 + (jaccard * 15)  # 45-60 range
                return score, "word_overlap", {"common_words": list(common), "jaccard": jaccard}
            elif len(common) == 1:
                # Single word match - only if it's significant
                matched_word = list(common)[0]
                if len(matched_word) >= 4:  # Ignore short common words
                    score = 35 + (jaccard * 10)  # 35-45 range
                    return score, "single_word", {"word": matched_word}
    
    return 0, "no_match", {}

# ============================================================================
# EXPERIENCE PARSING AND SCORING
# ============================================================================

def parse_experience_years(exp_str):
    '''Extract numeric years from experience string'''
    if not exp_str or str(exp_str).lower() in ['nan', 'none', 'n/a', '']:
        return None
    
    exp_str = str(exp_str).lower()
    
    # Pattern 1: "X years" or "X+ years"
    match = re.search(r'(\d+\.?\d*)\+?\s*(?:years?|yrs?)', exp_str)
    if match:
        return float(match.group(1))
    
    # Pattern 2: "X-Y years" (take average)
    match = re.search(r'(\d+\.?\d*)\s*-\s*(\d+\.?\d*)\s*(?:years?|yrs?)', exp_str)
    if match:
        return (float(match.group(1)) + float(match.group(2))) / 2
    
    # Pattern 3: Just a number
    match = re.search(r'(\d+\.?\d*)', exp_str)
    if match:
        return float(match.group(1))
    
    return None

def calculate_experience_score(candidate_exp, required_exp_range=None):
    '''
    Calculate experience match score (0-15 bonus points)
    
    Args:
        candidate_exp: Candidate's experience string
        required_exp_range: Tuple of (min_years, max_years) or single number
    '''
    if not required_exp_range:
        return 0
    
    cand_years = parse_experience_years(candidate_exp)
    if cand_years is None:
        return 0
    
    # Handle single number or range
    if isinstance(required_exp_range, (int, float)):
        min_years, max_years = required_exp_range, required_exp_range + 3
    elif isinstance(required_exp_range, (list, tuple)) and len(required_exp_range) == 2:
        min_years, max_years = required_exp_range
    else:
        return 0
    
    # Perfect match: within range
    if min_years <= cand_years <= max_years:
        return 15
    
    # Slightly overqualified (up to 2 years over)
    if cand_years > max_years and cand_years <= max_years + 2:
        return 12
    
    # Slightly underqualified (1 year under)
    if cand_years < min_years and cand_years >= min_years - 1:
        return 10
    
    # Moderately overqualified (2-5 years over)
    if cand_years > max_years + 2 and cand_years <= max_years + 5:
        return 8
    
    # Too junior or too senior
    return 0

# ============================================================================
# ENHANCED MATCHING ALGORITHM
# ============================================================================

def match_candidates_with_jd(required_skills, min_match_percentage=50, api_url=None, api_key=None, 
                              priority_skills=None, nice_to_have_skills=None, use_fuzzy=True, 
                              location_preference=None, required_experience=None, 
                              industry_preference=None, min_quality_threshold=40):
    '''
    Advanced candidate matching with multi-dimensional scoring
    
    Args:
        required_skills: List of mandatory skills
        min_match_percentage: Minimum match threshold (default 50%)
        priority_skills: Must-have critical skills (weighted 2x)
        nice_to_have_skills: Bonus skills (0.5x weight)
        use_fuzzy: Enable fuzzy matching (default True)
        location_preference: Preferred location(s) - can be list
        required_experience: (min, max) years or single number
        industry_preference: Preferred industry sectors
        min_quality_threshold: Minimum average quality score per skill (default 40)
    
    Returns:
        List of matched candidates with comprehensive scoring
    '''
    try:
        df = fetch_candidates_from_api(api_url, api_key)
        
        if df.empty:
            print("❌ No candidates found from API")
            return []
        
        if 'skills' not in df.columns:
            print("❌ Error: 'skills' column not found")
            print(f"Available columns: {df.columns.tolist()}")
            return []
        
        print(f"📊 Processing {len(df)} candidates")
        
        # Normalize inputs
        required_skills_lower = [normalize_skill(s) for s in required_skills if s.strip()]
        priority_skills_lower = [normalize_skill(s) for s in (priority_skills or [])]
        nice_to_have_lower = [normalize_skill(s) for s in (nice_to_have_skills or [])]
        
        if not required_skills_lower:
            print("❌ No valid required skills")
            return []
        
        # Combine all skills for comprehensive evaluation
        all_evaluated_skills = set(required_skills_lower + priority_skills_lower + nice_to_have_lower)
        
        print(f"🎯 Required skills ({len(required_skills_lower)})")
        print(f"⭐ Priority skills ({len(priority_skills_lower)})")
        print(f"➕ Nice-to-have skills ({len(nice_to_have_lower)})")
        
        matched_candidates = []
        
        for idx, row in df.iterrows():
            candidate_skills_str = str(row.get('skills', ''))
            
            if not candidate_skills_str or candidate_skills_str.lower() in ['nan', 'none', '']:
                continue
            
            # Parse and normalize candidate skills
            candidate_skills_raw = re.split(r'[,;|\n]', candidate_skills_str)
            candidate_skills = [normalize_skill(s) for s in candidate_skills_raw if s.strip()]
            
            if not candidate_skills:
                continue
            
            # === SKILL MATCHING WITH WEIGHTS ===
            matched_details = {}
            total_weighted_score = 0
            total_weight = 0
            
            # Required skills (weight: 1.0)
            for req_skill in required_skills_lower:
                best_score = 0
                best_match = None
                
                for cand_skill in candidate_skills:
                    score, match_type, details = calculate_skill_similarity(req_skill, cand_skill, use_fuzzy)
                    if score > best_score:
                        best_score = score
                        best_match = {'type': match_type, 'cand_skill': cand_skill, 'details': details}
                
                if best_score >= 40:  # Minimum threshold for match
                    matched_details[req_skill] = {
                        'score': best_score,
                        'weight': 1.0,
                        'category': 'required',
                        **best_match
                    }
                    total_weighted_score += best_score * 1.0
                    total_weight += 1.0
                else:
                    # Skill not found - penalize
                    total_weight += 1.0
            
            # Priority skills (weight: 2.0 - twice as important)
            for pri_skill in priority_skills_lower:
                if pri_skill in matched_details:
                    # Already matched as required, increase weight
                    matched_details[pri_skill]['weight'] = 2.0
                    matched_details[pri_skill]['category'] = 'priority'
                    total_weighted_score += matched_details[pri_skill]['score']  # Add bonus
                    total_weight += 1.0
                else:
                    # Not yet matched, try to match
                    best_score = 0
                    best_match = None
                    
                    for cand_skill in candidate_skills:
                        score, match_type, details = calculate_skill_similarity(pri_skill, cand_skill, use_fuzzy)
                        if score > best_score:
                            best_score = score
                            best_match = {'type': match_type, 'cand_skill': cand_skill, 'details': details}
                    
                    if best_score >= 40:
                        matched_details[pri_skill] = {
                            'score': best_score,
                            'weight': 2.0,
                            'category': 'priority',
                            **best_match
                        }
                        total_weighted_score += best_score * 2.0
                        total_weight += 2.0
                    else:
                        # Missing priority skill - major penalty
                        total_weight += 2.0
            
            # Nice-to-have skills (weight: 0.5 - bonus only)
            nice_to_have_matched = 0
            for nice_skill in nice_to_have_lower:
                if nice_skill not in matched_details:
                    best_score = 0
                    best_match = None
                    
                    for cand_skill in candidate_skills:
                        score, match_type, details = calculate_skill_similarity(nice_skill, cand_skill, use_fuzzy)
                        if score > best_score:
                            best_score = score
                            best_match = {'type': match_type, 'cand_skill': cand_skill, 'details': details}
                    
                    if best_score >= 40:
                        matched_details[nice_skill] = {
                            'score': best_score,
                            'weight': 0.5,
                            'category': 'nice_to_have',
                            **best_match
                        }
                        nice_to_have_matched += 1
                        # Bonus points (not penalized if missing)
                        total_weighted_score += best_score * 0.5
            
            # === CALCULATE SCORES ===
            
            # Base match percentage
            required_matched = sum(1 for s in required_skills_lower if s in matched_details)
            base_match_pct = (required_matched / len(required_skills_lower)) * 100 if required_skills_lower else 0
            
            # Quality score (weighted average)
            quality_score = (total_weighted_score / total_weight) if total_weight > 0 else 0
            
            # Check quality threshold
            if quality_score < min_quality_threshold:
                continue  # Skip low-quality matches
            
            # Combined score (60% base, 40% quality)
            combined_score = (base_match_pct * 0.6) + (quality_score * 0.4)
            
            # === BONUSES ===
            bonuses = {}
            
            # Priority skills bonus (up to 15%)
            if priority_skills_lower:
                priority_matched = sum(1 for s in priority_skills_lower if s in matched_details)
                priority_pct = (priority_matched / len(priority_skills_lower))
                priority_bonus = priority_pct * 15
                combined_score += priority_bonus
                bonuses['priority'] = round(priority_bonus, 1)
            
            # Nice-to-have bonus (up to 5%)
            if nice_to_have_lower:
                nice_bonus = (nice_to_have_matched / len(nice_to_have_lower)) * 5
                combined_score += nice_bonus
                bonuses['nice_to_have'] = round(nice_bonus, 1)
            
            # Experience bonus (up to 10%)
            if required_experience:
                exp_bonus = calculate_experience_score(row.get('experience'), required_experience) * (10/15)
                combined_score += exp_bonus
                bonuses['experience'] = round(exp_bonus, 1)
            
            # Location bonus (up to 5%)
            if location_preference:
                locations = location_preference if isinstance(location_preference, list) else [location_preference]
                cand_location = str(row.get('location', '')).lower()
                
                for loc in locations:
                    if loc.lower() in cand_location:
                        bonuses['location'] = 5
                        combined_score += 5
                        break
            
            # Cap at 100%
            combined_score = min(combined_score, 100)
            
            # === FILTER BY THRESHOLD ===
            if combined_score < min_match_percentage:
                continue
            
            # === CALCULATE METRICS ===
            exact_matches = sum(1 for d in matched_details.values() if d['type'] == 'exact')
            priority_matches = sum(1 for s in priority_skills_lower if s in matched_details)
            
            # Skill strength distribution
            matched_skills = list(matched_details.keys())
            skill_scores = [d['score'] for d in matched_details.values()]
            avg_skill_strength = np.mean(skill_scores) if skill_scores else 0
            
            candidate_data = {
                'id': row.get('id', 'N/A'),
                'name': row.get('name', 'N/A'),
                'email': row.get('email', 'N/A'),
                'contact': row.get('contact', 'N/A'),
                'location': row.get('location', 'N/A'),
                'current_company': row.get('current_company', 'N/A'),
                'designation': row.get('designation', 'N/A'),
                'experience': row.get('experience', 'N/A'),
                'linkedin': row.get('linkedin', 'N/A'),
                'qualification': row.get('qualification', 'N/A'),
                'skills': row.get('skills', 'N/A'),
                'cv_link': row.get('cv_link', 'N/A'),
                'status': row.get('status', 'Active'),
                
                # Scoring metrics
                'match_percentage': round(combined_score, 1),
                'quality_score': round(quality_score, 1),
                'base_match_percentage': round(base_match_pct, 1),
                'avg_skill_strength': round(avg_skill_strength, 1),
                
                # Skill details
                'matched_skills': matched_skills,
                'matched_skills_count': len(matched_skills),
                'total_required_skills': len(required_skills_lower),
                'exact_matches': exact_matches,
                'priority_matches': priority_matches,
                'nice_to_have_matches': nice_to_have_matched,
                'skill_match_details': matched_details,
                
                # Bonuses
                'bonuses': bonuses,
                'total_bonus': round(sum(bonuses.values()), 1)
            }
            
            matched_candidates.append(candidate_data)
        
        # === ADVANCED SORTING ===
        # Sort by: match_percentage (primary), priority_matches (secondary), quality_score (tertiary)
        matched_candidates.sort(
            key=lambda x: (
                x['match_percentage'],
                x['priority_matches'],
                x['quality_score'],
                x['exact_matches']
            ),
            reverse=True
        )
        
        print(f"\n✅ Found {len(matched_candidates)} matching candidates")
        
        if matched_candidates:
            print(f"\n🏆 Top 5 Candidates:")
            for i, c in enumerate(matched_candidates[:5], 1):
                print(f"{i}. {c['name']}")
                print(f"   Overall: {c['match_percentage']:.1f}% | Quality: {c['quality_score']:.1f}")
                print(f"   Required: {c['matched_skills_count']}/{c['total_required_skills']} | "
                      f"Priority: {c['priority_matches']} | Exact: {c['exact_matches']}")
                print(f"   Bonuses: {c['total_bonus']:.1f}%")
        
        return matched_candidates
    
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return []

# ============================================================================
# CANDIDATE FILTERING AND RANKING
# ============================================================================

def get_best_candidates(matched_candidates, top_n=10, diversity_mode=False, 
                       ensure_priority_match=True):
    '''
    Select best candidates with intelligent filtering
    
    Args:
        matched_candidates: List of matched candidates
        top_n: Number to return
        diversity_mode: Ensure company/location diversity
        ensure_priority_match: Only include candidates matching all priority skills
    '''
    if not matched_candidates:
        return []
    
    candidates = matched_candidates.copy()
    
    # Filter by priority skills if required
    if ensure_priority_match:
        candidates = [c for c in candidates if c.get('priority_matches', 0) == c.get('total_required_skills', 0)]
    
    if not diversity_mode:
        return candidates[:top_n]
    
    # Diversity selection
    selected = []
    companies = Counter()
    locations = Counter()
    max_per_company = max(1, top_n // 5)
    max_per_location = max(2, top_n // 3)
    
    for candidate in candidates:
        if len(selected) >= top_n:
            break
        
        company = candidate.get('current_company', 'N/A')
        location = candidate.get('location', 'N/A').split(',')[0]  # City only
        
        # Check diversity constraints
        if company != 'N/A' and companies[company] >= max_per_company:
            continue
        if locations[location] >= max_per_location:
            continue
        
        selected.append(candidate)
        companies[company] += 1
        locations[location] += 1
    
    # Fill remaining if needed
    if len(selected) < top_n:
        for candidate in candidates:
            if candidate not in selected:
                selected.append(candidate)
                if len(selected) >= top_n:
                    break
    
    return selected

# ============================================================================
# UTILITY FUNCTIONS (from original code)
# ============================================================================

def generate_linkedin_search_strings(skills, role_title, experience_level):
    '''Generate optimized LinkedIn Recruiter boolean search strings'''
    
    # Clean and prepare skills
    top_skills = skills[:15] if len(skills) > 15 else skills
    
    # Create different search string variations
    searches = {}
    
    # 1. Basic Boolean Search (AND)
    basic_and = " AND ".join([f'"{skill}"' for skill in top_skills[:8]])
    searches['basic_and'] = basic_and
    
    # 2. Flexible Boolean Search (OR for similar skills)
    if len(top_skills) >= 3:
        part1 = " OR ".join([f'"{skill}"' for skill in top_skills[:3]])
        part2 = " OR ".join([f'"{skill}"' for skill in top_skills[3:6]])
        flexible = f'({part1}) AND ({part2})' if part2 else f'({part1})'
        searches['flexible'] = flexible
    
    # 3. Title + Key Skills
    skills_part = " AND ".join([f'"{skill}"' for skill in top_skills[:5]])
    title_search = f'(title:"{role_title}") AND ({skills_part})'
    searches['with_title'] = title_search
    
    # 4. Simple comma-separated for LinkedIn Skills filter
    skills_filter = ", ".join(top_skills[:10])
    searches['skills_filter'] = skills_filter
    
    # 5. X-Ray Search (for Google/LinkedIn combination)
    xray_skills = " ".join([f'"{skill}"' for skill in top_skills[:6]])
    xray_search = f'site:linkedin.com/in/ "{role_title}" {xray_skills}'
    searches['xray'] = xray_search
    
    return searches

def extract_text_from_file(file_path):
    '''Extract text from TXT, PDF, or DOCX files'''
    ext = Path(file_path).suffix.lower()
    
    try:
        if ext == '.txt':
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()
        
        elif ext == '.pdf':
            reader = PdfReader(file_path)
            text = ""
            for page in reader.pages:
                text += page.extract_text() + "\n"
            return text
        
        elif ext == '.docx':
            doc = Document(file_path)
            return '\n'.join([para.text for para in doc.paragraphs])
    
    except Exception as e:
        print(f"Error extracting text from {file_path}: {e}")
        return ""
    
    return ""

def extract_skills_from_jd(jd_text, domain_hint=""):
    '''Extract ALL skills comprehensively from job description using OpenAI API'''
    try:
        client = OpenAI(api_key=settings.OPENAI_API_KEY)
    except Exception as e:
        print(f"Error initializing OpenAI client: {e}")
        return get_default_error_response()
    
    domain_context = f"The job is in the {domain_hint} domain." if domain_hint else ""
    
    prompt = f'''
You are an expert HR recruitment assistant AI. Carefully analyze the following Job Description and extract EVERY skill, technology, tool, qualification, and competency mentioned.

Return a structured JSON with:

1. "all_skills": A comprehensive list of ALL skills mentioned in the JD including:
   - Technical skills (programming languages, tools, frameworks, technologies)
   - Functional skills (domain-specific abilities)
   - Software/Tools (any applications, platforms, or systems)
   - Methodologies (Agile, Scrum, Six Sigma, etc.)
   - Certifications or qualifications mentioned
   - Domain knowledge areas
   - Soft skills (communication, leadership, teamwork, etc.)
   
   Extract 15-30 skills. Be thorough and don't miss anything mentioned in the JD.

2. "priority_skills": List 3-8 CRITICAL must-have skills that are absolutely essential for this role. These should be the skills mentioned multiple times or emphasized as requirements.

3. "nice_to_have_skills": List 5-10 bonus skills that would be beneficial but not mandatory.

4. "skill_categories": Organize the skills into categories like:
   {{"Technical": [...], "Tools": [...], "Soft Skills": [...], "Domain Knowledge": [...], "Certifications": [...]}}
   
5. "linkedin_optimized_skills": A list of 8-15 MOST IMPORTANT skills optimized for LinkedIn Recruiter search. 
   - Focus on searchable, industry-standard terms
   - Remove generic terms like "communication" or "teamwork"
   - Prioritize: specific technologies, tools, certifications, frameworks
   - Use exact names as they appear on LinkedIn (e.g., "JavaScript" not "JS", "Amazon Web Services (AWS)" not just "AWS")

6. "role_category": The most suitable role category (e.g., HR, Marketing, IT, Finance, Sales, Operations, etc.)

7. "experience_level": one of ["Entry Level", "Mid Level", "Senior Level", "Executive Level"]

8. "required_experience_years": Extract the experience requirement as a JSON object like {{"min": 3, "max": 5}} or {{"min": 5}} if only minimum is mentioned.

9. "key_responsibilities": List 5-7 main responsibilities mentioned in the JD

10. "qualifications": Educational requirements and certifications

11. "preferred_location": Extract any location preferences mentioned

{domain_context}

Be extremely thorough. If someone reads only your extracted skills, they should fully understand what this job requires.

Return ONLY valid JSON, no code block, no markdown, no explanation.

JD:
{jd_text[:4000]}
'''

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are an AI expert at extracting comprehensive skill requirements from job descriptions. Extract EVERY skill mentioned. Return only valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
            max_tokens=2000
        )

        content = response.choices[0].message.content.strip()
        
        # Clean content in case it includes markdown ```json``` wrapping
        cleaned = re.sub(r"^```json\s*|\s*```$", "", content, flags=re.MULTILINE).strip()

        try:
            result = json.loads(cleaned)
            
            # Validate and normalize expected keys
            if "all_skills" not in result:
                result["all_skills"] = []
            
            if "priority_skills" not in result:
                result["priority_skills"] = result.get("all_skills", [])[:5]
            
            if "nice_to_have_skills" not in result:
                result["nice_to_have_skills"] = []
            
            if "linkedin_optimized_skills" not in result:
                result["linkedin_optimized_skills"] = result.get("all_skills", [])[:10]
                
            if "skill_categories" not in result:
                result["skill_categories"] = {}
                
            if "role_category" not in result:
                result["role_category"] = "Unknown"
                
            if "experience_level" not in result:
                result["experience_level"] = "Unknown"
            
            if "required_experience_years" not in result:
                result["required_experience_years"] = {}
                
            if "key_responsibilities" not in result:
                result["key_responsibilities"] = []
                
            if "qualifications" not in result:
                result["qualifications"] = []
            
            if "preferred_location" not in result:
                result["preferred_location"] = None
            
            return result

        except json.JSONDecodeError as je:
            print(f"⚠️ JSON Decode Error: {je}")
            print(f"⚠️ Raw LLM Output: {content}")
            return get_default_error_response()

    except Exception as e:
        print(f"❌ Error calling OpenAI API: {e}")
        return get_default_error_response()

def get_default_error_response():
    '''Return default response when API fails'''
    return {
        "all_skills": ["Error extracting skills - please try again"],
        "priority_skills": [],
        "nice_to_have_skills": [],
        "skill_categories": {},
        "role_category": "Unknown",
        "experience_level": "Unknown",
        "required_experience_years": {},
        "key_responsibilities": [],
        "qualifications": [],
        "preferred_location": None
    }

def load_skills_map():
    '''Load skills mapping from JSON file or return default'''
    try:
        if settings.SKILLS_MAP_PATH.exists():
            with open(settings.SKILLS_MAP_PATH, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if not content:
                    print("⚠️ Skills map file is empty, using default")
                    return get_default_skills_map()
                return json.loads(content)
        else:
            print("⚠️ Skills map file not found, using default")
            return get_default_skills_map()
    except (json.JSONDecodeError, FileNotFoundError) as e:
        print(f"⚠️ Error loading skills map: {e}, using default")
        return get_default_skills_map()

def get_default_skills_map():
    '''Return comprehensive default skills mapping'''
    return {
        "Docker": ["Kubernetes", "AWS ECS", "Containerization", "Docker Compose", "CI/CD"],
        "Python": ["Django", "Flask", "FastAPI", "NumPy", "Pandas", "Data Science"],
        "JavaScript": ["React", "Node.js", "TypeScript", "Vue.js", "Angular"],
        "Recruitment": ["Talent Acquisition", "Interviewing", "Onboarding", "ATS", "Sourcing"],
        "Marketing": ["SEO", "Content Strategy", "Campaign Management", "Google Analytics", "Social Media"],
        "Finance": ["Budgeting", "Forecasting", "Financial Modelling", "Excel", "Accounting"],
        "SQL": ["Database Design", "PostgreSQL", "MySQL", "Data Analysis", "Query Optimization"],
        "Project Management": ["Agile", "Scrum", "JIRA", "Stakeholder Management", "Risk Management"],
        "Sales": ["CRM", "Lead Generation", "Negotiation", "Account Management", "Pipeline Management"],
        "HR": ["Employee Relations", "Performance Management", "HRMS", "Compliance", "Training"],
        "Java": ["Spring Boot", "Hibernate", "Maven", "JUnit", "Microservices"],
        "AWS": ["EC2", "S3", "Lambda", "CloudFormation", "RDS"],
        "Data Analysis": ["Excel", "Tableau", "Power BI", "Statistics", "SQL"],
        "Content Writing": ["Copywriting", "SEO Writing", "Editing", "Blogging", "Content Strategy"],
        "Customer Service": ["Communication", "Problem Solving", "CRM", "Ticketing Systems", "Customer Support"],
        "Machine Learning": ["TensorFlow", "PyTorch", "Scikit-learn", "Deep Learning", "NLP"],
        "DevOps": ["Jenkins", "Docker", "Kubernetes", "Terraform", "Monitoring"],
        "UI/UX": ["Figma", "Adobe XD", "Wireframing", "Prototyping", "User Research"],
        "Product Management": ["Roadmap Planning", "User Stories", "Product Strategy", "Analytics", "Stakeholder Management"]
    }

def expand_skills_with_map(primary_skills, secondary_skills):
    '''Expand secondary skills based on primary skills using skills map'''
    skills_map = load_skills_map()
    expanded_secondary = set(secondary_skills) if secondary_skills else set()
    
    for skill in primary_skills:
        # Check exact match
        if skill in skills_map:
            expanded_secondary.update(skills_map[skill])
        # Check case-insensitive match
        else:
            for map_skill, related in skills_map.items():
                if skill.lower() == map_skill.lower():
                    expanded_secondary.update(related)
                    break
    
    return list(expanded_secondary)

def delete_file_after_delay(file_path, delay_seconds=5):
    '''Delete a file after delay in background thread'''
    import time
    import threading
    
    def delete_file():
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                print(f"✅ Deleted file: {file_path}")
        except Exception as e:
            print(f"⚠️ Could not delete file {file_path}: {e}")
    
    if delay_seconds > 0:
        thread = threading.Thread(target=delete_file)
        thread.daemon = True
        thread.start()
    else:
        delete_file()


def cleanup_old_matched_files(days=1):
    '''Delete matched candidate files older than specified days'''
    from datetime import datetime, timedelta
    
    matched_dir = Path(settings.MEDIA_ROOT) / "matched_candidates"
    if not matched_dir.exists():
        return
    
    cutoff_time = datetime.now() - timedelta(days=days)
    deleted_count = 0
    
    for file_path in matched_dir.glob('*.xlsx'):
        if datetime.fromtimestamp(file_path.stat().st_mtime) < cutoff_time:
            try:
                file_path.unlink()
                deleted_count += 1
                print(f"✅ Deleted old file: {file_path.name}")
            except Exception as e:
                print(f"⚠️ Could not delete {file_path.name}: {e}")
    
    if deleted_count > 0:
        print(f"✅ Cleanup complete: {deleted_count} old files deleted")


def fetch_candidates_from_api(api_url=None, api_key=None, timeout=40):
    '''
    Fetch candidate data from API with improved debugging
    '''
    try:
        # Use settings if not provided
        if api_url is None:
            api_url = getattr(settings, 'CANDIDATES_API_URL', None)
        if api_key is None:
            api_key = getattr(settings, 'CANDIDATES_API_KEY', None)
        
        if not api_url:
            print("❌ Error: CANDIDATES_API_URL not configured in settings")
            return pd.DataFrame()
        
        # Prepare headers
        headers = {
            'Content-Type': 'application/json',
        }
        
        # Add API key to headers if provided
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'
        
        print(f"🔄 Fetching candidates from API: {api_url}")
        
        # Make API request
        response = requests.get(api_url, headers=headers, timeout=timeout)
        response.raise_for_status()
        
        # Parse JSON response
        data = response.json()
        
        print(f"📦 API Response type: {type(data)}")
        
        # Handle different API response structures
        if isinstance(data, dict):
            print(f"📦 Response keys: {list(data.keys())}")
            
            # Check for columnar format FIRST (highest priority)
            if 'cols' in data and 'data' in data:
                # Handle columnar format (cols + data)
                print(f"✅ Using columnar format (cols + data)")
                df = pd.DataFrame(data['data'], columns=data['cols'])
                print(f"✅ Successfully fetched {len(df)} candidates from API")
                return normalize_candidate_dataframe(df)
            elif 'cols' in data and 'rows' in data:
                # Handle columnar format (cols + rows)
                print(f"✅ Using columnar format (cols + rows)")
                df = pd.DataFrame(data['rows'], columns=data['cols'])
                print(f"✅ Successfully fetched {len(df)} candidates from API")
                return normalize_candidate_dataframe(df)
            # Check for common data wrapper keys
            elif 'data' in data:
                candidates = data['data']
                print(f"✅ Using 'data' key from response")
            elif 'candidates' in data:
                candidates = data['candidates']
                print(f"✅ Using 'candidates' key from response")
            elif 'results' in data:
                candidates = data['results']
                print(f"✅ Using 'results' key from response")
            else:
                # Assume the entire dict is the data
                candidates = data
                print(f"⚠️ Using entire response as data")
        elif isinstance(data, list):
            candidates = data
            print(f"✅ Response is a list with {len(data)} items")
        else:
            print(f"❌ Unexpected API response format: {type(data)}")
            return pd.DataFrame()
        
        # Convert to DataFrame
        if not candidates:
            print("⚠️ No candidates found in API response")
            return pd.DataFrame()
        
        # If candidates is not a list, try to handle it
        if not isinstance(candidates, list):
            print(f"⚠️ Candidates is not a list, type: {type(candidates)}")
            return pd.DataFrame()
        
        print(f"📊 Creating DataFrame from {len(candidates)} candidates")
        df = pd.DataFrame(candidates)
        
        # Normalize column names
        df = normalize_candidate_dataframe(df)
        
        print(f"✅ Successfully fetched {len(df)} candidates from API")
        return df
    
    except requests.exceptions.Timeout:
        print(f"❌ API request timed out after {timeout} seconds")
        return pd.DataFrame()
    except requests.exceptions.RequestException as e:
        print(f"❌ Error fetching candidates from API: {e}")
        return pd.DataFrame()
    except json.JSONDecodeError as e:
        print(f"❌ Error parsing API response JSON: {e}")
        print(f"❌ Response content (first 500 chars): {response.text[:500]}")
        return pd.DataFrame()
    except Exception as e:
        print(f"❌ Unexpected error fetching candidates: {e}")
        import traceback
        traceback.print_exc()
        return pd.DataFrame()

def normalize_candidate_dataframe(df):
    '''
    Normalize candidate DataFrame columns to standard format
    
    API fields mapping:
    c_id -> id
    c_name -> name
    c_email -> email
    c_phone -> contact
    c_loc -> location
    c_l_url -> linkedin
    c_exp -> experience
    c_skills -> skills
    c_qualifications -> qualification
    c_designation -> designation
    c_cv_url -> cv_link
    '''
    
    print(f"📋 Original columns: {df.columns.tolist()}")
    
    # Define column mapping
    column_mapping = {
        'c_id': 'id',
        'c_name': 'name',
        'c_email': 'email',
        'c_phone': 'contact',
        'c_loc': 'location',
        'c_l_url': 'linkedin',
        'c_exp': 'experience',
        'c_skills': 'skills',
        'c_qualifications': 'qualification',
        'c_designation': 'designation',
        'c_cv_url': 'cv_link'
    }
    
    # Rename columns based on mapping (only if they exist)
    rename_dict = {old: new for old, new in column_mapping.items() if old in df.columns}
    if rename_dict:
        df = df.rename(columns=rename_dict)
        print(f"✅ Renamed columns: {rename_dict}")
    
    # Define standard columns we need
    standard_columns = [
        'id', 'name', 'email', 'contact', 'location', 
        'linkedin', 'experience', 'skills', 'qualification', 
        'designation', 'cv_link'
    ]
    
    # Add missing standard columns with N/A
    for col in standard_columns:
        if col not in df.columns:
            df[col] = 'N/A'
            print(f"⚠️ Added missing column: {col}")
    
    # Add placeholder columns for compatibility
    if 'current_company' not in df.columns:
        df['current_company'] = 'N/A'
    if 'status' not in df.columns:
        df['status'] = 'Active'
    
    print(f"✅ Final columns: {df.columns.tolist()}")
    
    # Show sample data for debugging
    if not df.empty:
        print(f"📊 Sample data (first row):")
        sample = df.iloc[0]
        print(f"   - Name: {sample.get('name', 'N/A')}")
        print(f"   - Email: {sample.get('email', 'N/A')}")
        print(f"   - Skills: {sample.get('skills', 'N/A')[:100]}...")  # First 100 chars
    
    return df

def export_matched_candidates(matched_candidates, output_path):
    '''
    Export matched candidates to Excel
    
    Args:
        matched_candidates: List of matched candidate dictionaries
        output_path: Path where Excel file will be saved
    
    Returns:
        Boolean indicating success
    '''
    try:
        if not matched_candidates:
            print("⚠️ No candidates to export")
            return False
        
        df = pd.DataFrame(matched_candidates)
        
        # Reorder columns for better readability
        column_order = [
            'match_percentage', 'quality_score', 'matched_skills_count', 'total_required_skills',
            'exact_matches', 'priority_matches', 'name', 'email', 'contact', 
            'designation', 'current_company', 'experience', 'location', 
            'qualification', 'linkedin', 'skills', 'matched_skills', 
            'cv_link', 'status', 'id'
        ]
        
        # Only include columns that exist
        column_order = [col for col in column_order if col in df.columns]
        df = df[column_order]
        
        # Ensure output directory exists
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        
        # Export to Excel
        df.to_excel(output_path, index=False, engine='openpyxl')
        print(f"✅ Matched candidates exported to: {output_path}")
        return True
    
    except Exception as e:
        print(f"❌ Error exporting matched candidates: {e}")
        return False


def save_jd_to_excel(jd_data):
    '''
    Save job description data to Excel database
    
    Args:
        jd_data: Dictionary containing job description information
    '''
    try:
        excel_path = Path(settings.EXCEL_DATABASE_PATH)
        data_dir = excel_path.parent
        
        df_new = pd.DataFrame([jd_data])
        
        if excel_path.exists():
            try:
                df_existing = pd.read_excel(excel_path)
                df_combined = pd.concat([df_existing, df_new], ignore_index=True)
            except Exception as e:
                print(f"⚠️ Error reading existing Excel file: {e}, creating new one")
                df_combined = df_new
        else:
            df_combined = df_new
        
        # Ensure data directory exists
        data_dir.mkdir(parents=True, exist_ok=True)
        
        # Save to Excel
        df_combined.to_excel(excel_path, index=False, engine='openpyxl')
        print(f"✅ Successfully saved JD data to Excel: {excel_path}")
        
    except Exception as e:
        print(f"❌ Error saving JD data to Excel: {e}")