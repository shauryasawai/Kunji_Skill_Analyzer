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
# CACHES AND CONSTANTS
# ============================================================================

DESIGNATION_CACHE = {}
SKILL_RELEVANCE_CACHE = {}




GENERIC_SOFT_SKILLS = {
    "communication", "leadership", "teamwork", "problem solving", 
    "collaboration", "presentation", "people management",
    "stakeholder management"
}

# ============================================================================
# CORE UTILITY FUNCTIONS
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

def generate_role_keyword_profile(jd_role_title: str):
    """Extract domain-relevant keywords from role title."""
    if not jd_role_title:
        return []

    jd = jd_role_title.lower()
    remove_words = {
        "senior", "jr", "junior", "lead", "manager", "associate", "intern",
        "executive", "specialist", "head", "chief", "vp", "director"
    }

    words = re.split(r"[ /,|-]+", jd)
    keywords = [w.strip() for w in words if len(w) > 2 and w not in remove_words]

    return keywords

# ============================================================================
# AI-POWERED ANALYSIS FUNCTIONS
# ============================================================================

def call_openai_analysis(prompt, system_message, temperature=0.2, max_tokens=300):
    """Unified OpenAI API call with error handling"""
    try:
        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt}
            ],
            temperature=temperature,
            max_tokens=max_tokens
        )
        
        content = response.choices[0].message.content.strip()
        cleaned = re.sub(r"^```json\s*|\s*```$", "", content, flags=re.MULTILINE).strip()
        
        return json.loads(cleaned)
    
    except json.JSONDecodeError as je:
        print(f"⚠️ JSON Decode Error: {je}")
        print(f"⚠️ Raw Output: {content}")
        return {}
    except Exception as e:
        print(f"⚠️ OpenAI API error: {e}")
        return {}

def is_designation_relevant(jd_role, candidate_designation, threshold=30):
    '''Use OpenAI to check if candidate designation is relevant to JD role - BALANCED VERSION'''
    if not jd_role or not candidate_designation:
        return True
    
    jd_role = str(jd_role).lower().strip()
    cand_desg = str(candidate_designation).lower().strip()
    
    if cand_desg in ['nan', 'none', 'n/a', '']:
        return True
    
    # Quick exact/substring match
    if jd_role == cand_desg or jd_role in cand_desg or cand_desg in jd_role:
        return True
    
    # AI analysis for complex cases - BALANCED PROMPT
    prompt = f'''You are an expert HR screening assistant. Determine if a candidate's designation is relevant for a job role.

Job Role: "{jd_role}"
Candidate Designation: "{cand_desg}"

Be REASONABLY STRICT but FAIR:
- Exact match or close variations = RELEVANT (e.g., "Software Engineer" for "Senior Software Engineer")
- Same function, different level = RELEVANT (e.g., "Junior Developer" for "Developer")
- Adjacent roles in same domain = RELEVANT (e.g., "Frontend Developer" for "Full Stack Developer")
- Related but different specialization = MAYBE RELEVANT (e.g., "Mobile Developer" for "Web Developer")
- Different function, same industry = BORDERLINE (consider on case-by-case)
- Completely different domain/function = NOT RELEVANT (e.g., "Graphic Designer" for "Backend Engineer")

Examples:
- "UI Designer" for "UX Designer" = RELEVANT
- "Data Analyst" for "Data Scientist" = RELEVANT (adjacent)
- "Backend Developer" for "Frontend Developer" = BORDERLINE (consider if full-stack context)
- "Designer" for "Developer" = NOT RELEVANT (unless specific context suggests otherwise)

Return ONLY valid JSON (no markdown, no code blocks):
{{
    "is_relevant": <true|false>,
    "confidence": "<high|medium|low>",
    "reason": "<one sentence explanation>",
    "match_strength": "<strong|moderate|weak|none>"
}}'''

    result = call_openai_analysis(
        prompt, 
        "You are an HR screening expert. Be balanced - approve good matches but filter poor fits. Return only valid JSON.",
        temperature=0.15,
        max_tokens=150
    )
    
    is_relevant = result.get('is_relevant', True)
    match_strength = result.get('match_strength', 'moderate')
    
    # Only reject if clearly not relevant with high confidence
    if not is_relevant and result.get('confidence') == 'high':
        print(f"   ⚠️ Filtered out: {cand_desg} for {jd_role} - {result.get('reason', 'N/A')}")
        return False
    
    # Allow through if moderate or weak match (let skill matching decide)
    return True

def calculate_skill_relevance_score(matched_skills, jd_role_title, candidate_designation, use_cache=True):
    '''Use OpenAI to calculate how relevant the matched skills are to the role - BALANCED VERSION'''
    if not matched_skills:
        return 0
    
    if not jd_role_title:
        return 50
    
    # Create cache key
    skills_key = ",".join(sorted(matched_skills[:10]))
    cache_key = f"{jd_role_title}||{skills_key}"
    
    if use_cache and cache_key in SKILL_RELEVANCE_CACHE:
        return SKILL_RELEVANCE_CACHE[cache_key]
    
    skills_str = ", ".join(matched_skills[:15])
    
    prompt = f'''You are an expert technical recruiter analyzing skill-to-role fit.

Job Role: "{jd_role_title}"
Candidate's Current Designation: "{candidate_designation}"
Matched Skills: {skills_str}

Analyze how RELEVANT these matched skills are for this job role. Be FAIR but DISCERNING.

SCORING GUIDELINES:
- 80-100: Excellent - strong core skills directly relevant to role
- 60-79: Good - relevant skills with some domain match
- 45-59: Moderate - some relevant skills, decent transferability
- 30-44: Fair - limited relevance, mostly generic or adjacent
- 0-29: Poor - wrong domain or no relevant skills

BE BALANCED:
- For "UI/UX Designer": Figma, Sketch = HIGH (80+); Web Design = MEDIUM (60-70); Graphic Design = FAIR (45-55); Backend skills = LOW (20-30)
- For "Backend Engineer": Python, Django, PostgreSQL = HIGH (80+); JavaScript, React = MEDIUM (50-65); UI/UX = LOW (20-30)
- For "Full Stack Developer": Both frontend and backend = HIGH (80+); Only frontend or backend = MEDIUM (60-75)
- Adjacent/transferable skills deserve 45-65 range, not below 30
- Generic soft skills alone score below 35

DOMAIN CONSIDERATIONS:
- Cross-domain skills from related areas: 45-60 range (e.g., Mobile dev for Web dev role)
- Completely different domain: below 35 (e.g., Design skills for Backend role)

Return ONLY valid JSON (no markdown, no code blocks):
{{
    "relevance_score": <number 0-100>,
    "quality": "<high|medium|low>",
    "reasoning": "<2-3 sentence explanation>",
    "core_skills_present": <true|false>,
    "domain_match": <true|false>,
    "red_flags": ["<list any major concerns>"]
}}'''

    result = call_openai_analysis(
        prompt,
        "You are an expert recruiter. Be fair and balanced - reward relevant skills but differentiate quality. Return only valid JSON.",
        temperature=0.2,
        max_tokens=400
    )
    
    relevance_score = float(result.get('relevance_score', 50))
    relevance_score = max(0, min(100, relevance_score))
    
    if relevance_score < 45:
        print(f"   ⚠️ Low skill relevance ({relevance_score:.1f}): {result.get('reasoning', 'N/A')}")
        if result.get('red_flags'):
            print(f"      Red flags: {', '.join(result.get('red_flags', []))}")
    
    if use_cache:
        SKILL_RELEVANCE_CACHE[cache_key] = relevance_score
    
    return relevance_score

def calculate_designation_similarity(jd_role, candidate_designation, use_fuzzy=True, use_cache=True):
    '''Calculate similarity between JD role and candidate designation using OpenAI (0-100)'''
    if not jd_role or not candidate_designation:
        return 0, "no_data", {}
    
    jd_role = str(jd_role).lower().strip()
    cand_desg = str(candidate_designation).lower().strip()
    
    if not jd_role or not cand_desg or cand_desg in ['nan', 'none', 'n/a', '']:
        return 0, "no_data", {}
    
    # Quick exact match check
    if jd_role == cand_desg:
        return 100, "exact", {"matched": "exact designation match"}
    
    # Check cache
    cache_key = f"{jd_role}||{cand_desg}"
    if use_cache and cache_key in DESIGNATION_CACHE:
        cached_result = DESIGNATION_CACHE[cache_key]
        cached_result[2]['cache_hit'] = True
        return cached_result
    
    # AI analysis
    prompt = f'''You are an expert HR analyst specializing in job role matching across all industries.

Compare these two job designations and determine how similar they are:

Job Description Role: "{jd_role}"
Candidate's Current Designation: "{cand_desg}"

Return ONLY a valid JSON object with this exact structure (no markdown, no code blocks):
{{
    "similarity_score": <number 0-100>,
    "match_type": "<exact|high|moderate|low|no_match>",
    "confidence": "<high|medium|low>",
    "reasoning": "<brief 1-2 sentence explanation>",
    "seniority_match": <true|false>,
    "function_match": <true|false>,
    "role_equivalent": <true|false>
}}'''

    result = call_openai_analysis(
        prompt,
        "You are an expert HR analyst. Analyze job role similarity and return only valid JSON.",
        temperature=0.1,
        max_tokens=300
    )
    
    if result:
        score = float(result.get('similarity_score', 0))
        score = max(0, min(100, score))
        
        match_type = result.get('match_type', 'unknown')
        
        details = {
            'reasoning': result.get('reasoning', 'No reasoning provided'),
            'confidence': result.get('confidence', 'unknown'),
            'seniority_match': result.get('seniority_match', False),
            'function_match': result.get('function_match', False),
            'role_equivalent': result.get('role_equivalent', False),
            'api_source': 'openai',
            'cache_hit': False
        }
        
        result_tuple = (score, match_type, details)
        if use_cache:
            DESIGNATION_CACHE[cache_key] = result_tuple
        
        return result_tuple
    
    # Fallback to fuzzy matching if AI fails
    return fallback_designation_matching(jd_role, cand_desg, use_cache, cache_key)

def fallback_designation_matching(jd_role, cand_desg, use_cache, cache_key):
    '''Fallback designation matching using fuzzy logic'''
    print(f"⚠️ Using fallback fuzzy matching for: {jd_role} vs {cand_desg}")
    
    # Substring match
    if jd_role in cand_desg or cand_desg in jd_role:
        result = (90, "substring", {"matched": "substring match (fallback)"})
        if use_cache:
            DESIGNATION_CACHE[cache_key] = result
        return result
    
    # Fuzzy string matching
    token_sort = fuzz.token_sort_ratio(jd_role, cand_desg)
    partial = fuzz.partial_ratio(jd_role, cand_desg)
    token_set = fuzz.token_set_ratio(jd_role, cand_desg)
    
    best_fuzzy = max(token_sort, partial, token_set)
    
    if best_fuzzy >= 80:
        score = 70 + ((best_fuzzy - 80) * 1.5)
        result = (score, "fuzzy_high", {"similarity": best_fuzzy, "source": "fallback"})
    elif best_fuzzy >= 70:
        score = 50 + ((best_fuzzy - 70) * 2)
        result = (score, "fuzzy_medium", {"similarity": best_fuzzy, "source": "fallback"})
    elif best_fuzzy >= 60:
        score = 30 + ((best_fuzzy - 60) * 2)
        result = (score, "fuzzy_low", {"similarity": best_fuzzy, "source": "fallback"})
    else:
        # Word overlap
        jd_words = set(jd_role.split())
        cand_words = set(cand_desg.split())
        
        stopwords = {'and', 'or', 'the', 'a', 'an', 'of', 'in', 'to', 'for', 'with', 'on', 'at', 'by'}
        jd_words_clean = jd_words - stopwords
        cand_words_clean = cand_words - stopwords
        
        if jd_words_clean and cand_words_clean:
            common = jd_words_clean & cand_words_clean
            if common:
                union = jd_words_clean | cand_words_clean
                jaccard = len(common) / len(union)
                score = min(40, jaccard * 60)
                result = (score, "word_overlap", {
                    "common_words": list(common),
                    "jaccard": jaccard,
                    "source": "fallback"
                })
            else:
                result = (0, "no_match", {"source": "fallback"})
        else:
            result = (0, "no_match", {"source": "fallback"})
    
    if use_cache:
        DESIGNATION_CACHE[cache_key] = result
    return result

# ============================================================================
# SKILL MATCHING ENGINE
# ============================================================================
def calculate_skill_similarity(req_skill, cand_skill, use_fuzzy=True):
    '''Direct exact matching only - 100 or 0'''
    req_norm = normalize_skill(req_skill)
    cand_norm = normalize_skill(cand_skill)
    
    # Exact match only
    if req_norm == cand_norm:
        return 100, "exact", {"matched": "exact match"}
    
    # Optional: Substring match for compound skills
    if req_norm in cand_norm or cand_norm in req_norm:
        return 85, "substring", {"matched": "partial match"}
    
    return 0, "no_match", {}

def normalize_skill(skill):
    '''
    Enhanced normalization for better matching
    - Lowercase
    - Remove special characters (/,-,etc.)
    - Collapse multiple spaces
    - Strip whitespace
    '''
    if not skill:
        return ""
    
    skill = str(skill).lower().strip()
    
    # Remove special characters but keep spaces
    skill = re.sub(r'[^\w\s]', ' ', skill)
    
    # Collapse multiple spaces
    skill = ' '.join(skill.split())
    
    return skill

def skill_tokens(skill):
    '''Extract meaningful word tokens from skill (2+ chars)'''
    normalized = normalize_skill(skill)
    return {word for word in normalized.split() if len(word) >= 2}

def extract_skill_versions(skill):
    '''Extract version numbers from skills'''
    version_pattern = r'(\d+(?:\.\d+)*(?:\.[xX])?)'
    match = re.search(version_pattern, skill)
    
    if match:
        base_skill = re.sub(version_pattern, '', skill).strip()
        version = match.group(1)
        return base_skill, version
    
    return skill, None

def calculate_skill_similarity(req_skill, cand_skill, use_fuzzy=True):
    '''
    Smart skill matching with multiple strategies:
    1. Exact match (100)
    2. Substring match (85)
    3. Token overlap match (70)
    4. No match (0)
    '''
    req_norm = normalize_skill(req_skill)
    cand_norm = normalize_skill(cand_skill)
    
    if not req_norm or not cand_norm:
        return 0, "no_match", {}
    
    # Strategy 1: Exact match
    if req_norm == cand_norm:
        return 100, "exact", {"matched": "exact match"}
    
    # Strategy 2: Substring match (one contains the other)
    if req_norm in cand_norm or cand_norm in req_norm:
        return 85, "substring", {"matched": "substring match"}
    
    # Strategy 3: Token overlap match
    # (e.g., "ui ux design" matches "ui design" or "ux")
    req_tokens = skill_tokens(req_skill)
    cand_tokens = skill_tokens(cand_skill)
    
    if req_tokens and cand_tokens:
        common_tokens = req_tokens & cand_tokens
        
        if common_tokens:
            # Calculate overlap percentage
            overlap_ratio = len(common_tokens) / min(len(req_tokens), len(cand_tokens))
            
            if overlap_ratio >= 0.8:  # 80%+ overlap
                return 75, "high_token_overlap", {
                    "common_tokens": list(common_tokens),
                    "overlap": f"{overlap_ratio:.0%}"
                }
            elif overlap_ratio >= 0.5:  # 50%+ overlap
                return 65, "medium_token_overlap", {
                    "common_tokens": list(common_tokens),
                    "overlap": f"{overlap_ratio:.0%}"
                }
            elif len(common_tokens) >= 1 and len(list(common_tokens)[0]) >= 4:
                # At least one meaningful word (4+ chars) matches
                return 55, "partial_token_match", {
                    "common_tokens": list(common_tokens)
                }
    
    return 0, "no_match", {}

def calculate_experience_score(candidate_exp, required_exp_range=None):
    '''Calculate experience match score (0-15 bonus points)'''
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
    
    # Scoring logic
    if min_years <= cand_years <= max_years:
        return 15
    elif cand_years > max_years and cand_years <= max_years + 2:
        return 12
    elif cand_years < min_years and cand_years >= min_years - 1:
        return 10
    elif cand_years > max_years + 2 and cand_years <= max_years + 5:
        return 8
    else:
        return 0

# ============================================================================
# MAIN MATCHING ALGORITHM
# ============================================================================
def fetch_candidates_from_api(api_url=None, api_key=None, timeout=60):
    '''Fetch candidate data from API with improved debugging'''
    # Keep the original implementation exactly as is
    try:
        if api_url is None:
            api_url = getattr(settings, 'CANDIDATES_API_URL', None)
        if api_key is None:
            api_key = getattr(settings, 'CANDIDATES_API_KEY', None)
        
        if not api_url:
            print("❌ Error: CANDIDATES_API_URL not configured in settings")
            return pd.DataFrame()
        
        headers = {'Content-Type': 'application/json'}
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'
        
        print(f"🔄 Fetching candidates from API: {api_url}")
        response = requests.get(api_url, headers=headers, timeout=timeout)
        response.raise_for_status()
        
        data = response.json()
        print(f"📦 API Response type: {type(data)}")
        
        # Handle different API response structures
        if isinstance(data, dict):
            print(f"📦 Response keys: {list(data.keys())}")
            
            if 'cols' in data and 'data' in data:
                print(f"✅ Using columnar format (cols + data)")
                df = pd.DataFrame(data['data'], columns=data['cols'])
                print(f"✅ Successfully fetched {len(df)} candidates from API")
                return normalize_candidate_dataframe(df)
            elif 'cols' in data and 'rows' in data:
                print(f"✅ Using columnar format (cols + rows)")
                df = pd.DataFrame(data['rows'], columns=data['cols'])
                print(f"✅ Successfully fetched {len(df)} candidates from API")
                return normalize_candidate_dataframe(df)
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
                candidates = data
                print(f"⚠️ Using entire response as data")
        elif isinstance(data, list):
            candidates = data
            print(f"✅ Response is a list with {len(data)} items")
        else:
            print(f"❌ Unexpected API response format: {type(data)}")
            return pd.DataFrame()
        
        if not candidates:
            print("⚠️ No candidates found in API response")
            return pd.DataFrame()
        
        if not isinstance(candidates, list):
            print(f"⚠️ Candidates is not a list, type: {type(candidates)}")
            return pd.DataFrame()
        
        print(f"📊 Creating DataFrame from {len(candidates)} candidates")
        df = pd.DataFrame(candidates)
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
        return pd.DataFrame()
    except Exception as e:
        print(f"❌ Unexpected error fetching candidates: {e}")
        import traceback
        traceback.print_exc()
        return pd.DataFrame()
    
def match_candidates_with_jd(required_skills=['all_skills'], min_match_percentage=15, api_url=None, api_key=None,  # ULTRA LOW: 15
                             priority_skills=None, nice_to_have_skills=None, use_fuzzy=True, 
                             location_preference=None, required_experience=None,
                             min_required_skills_match=None,
                             industry_preference=None, 
                             min_quality_threshold=5,  # ULTRA LOW: 5
                             jd_role_title=None,
                             debug_mode=True):
    '''
    ULTRA LENIENT VERSION: Will show candidates with even 1 skill match
    '''
    try:
        df = fetch_candidates_from_api(api_url, api_key)
        
        if df.empty:
            print("❌ No candidates found from API")
            return []
        
        if 'skills' not in df.columns:
            print("❌ Error: 'skills' column not found")
            return []
        
        print(f"📊 Processing {len(df)} candidates")
        
        # Normalize inputs
        required_skills_lower = [normalize_skill(s) for s in required_skills if s.strip()]
        priority_skills_lower = [normalize_skill(s) for s in (priority_skills or [])]
        nice_to_have_lower = [normalize_skill(s) for s in (nice_to_have_skills or [])]
        
        if not required_skills_lower:
            print("❌ No valid required skills")
            return []
        
        # ULTRA LENIENT: Accept even 1 skill match
        if min_required_skills_match is None:
            min_req_skills = 1  # CHANGED: Always accept 1+ skills
        else:
            min_req_skills = max(1, min_required_skills_match)  # CHANGED: Minimum is 1
        
        print(f"🎯 Required skills: {len(required_skills_lower)}")
        print(f"🎯 Minimum skills to match: {min_req_skills} (ULTRA LENIENT - accepting 1+ skills)")
        
        if debug_mode:
            print(f"🔍 DEBUG: First 10 required skills: {required_skills_lower[:10]}")
            print(f"🔍 DEBUG: Sample candidate skills from first row:")
            if not df.empty:
                sample_skills = str(df.iloc[0].get('skills', ''))[:200]
                print(f"   {sample_skills}...")
        
        matched_candidates = []
        filtered_count = defaultdict(int)
        
        for idx, row in df.iterrows():
            candidate_data = process_candidate(
                row, required_skills_lower, priority_skills_lower, nice_to_have_lower,
                jd_role_title, min_req_skills, min_quality_threshold, min_match_percentage,
                location_preference, required_experience, filtered_count
            )
            
            if candidate_data:
                matched_candidates.append(candidate_data)
        
        # Sort and display results
        return finalize_results(matched_candidates, filtered_count, len(df), min_match_percentage, min_req_skills)
    
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return []

def calculate_min_required_skills(jd_role_title, required_skills, min_override):
    '''Calculate minimum required skills - ULTRA LENIENT: Always 1'''
    if min_override is not None:
        return max(1, min_override)  # CHANGED: Never less than 1
    
    # CHANGED: Always return 1 for ultra-lenient matching
    return 1

def process_candidate(row, required_skills, priority_skills, nice_to_have_skills,
                     jd_role_title, min_req_skills, min_quality_threshold, 
                     min_match_percentage, location_preference, required_experience, filtered_count):
    '''Process a single candidate - ULTRA LENIENT VERSION'''
    candidate_skills_str = str(row.get('skills', ''))
    
    if not candidate_skills_str or candidate_skills_str.lower() in ['nan', 'none', '']:
        return None
    
    # REMOVED: Designation filter - accept all designations
    
    # Parse and filter candidate skills
    candidate_skills = parse_candidate_skills(candidate_skills_str)
    if not candidate_skills:
        return None
    
    # Skill matching with ULTRA LOW thresholds
    matched_details, total_weighted_score, total_weight, nice_to_have_matched = match_skills(
        candidate_skills, required_skills, priority_skills, nice_to_have_skills, jd_role_title
    )
    
    required_matched = sum(1 for s in required_skills if s in matched_details)
    
    # Debug print
    if required_matched > 0:
        print(f"   Candidate: {row.get('name', 'Unknown')} - Matched {required_matched}/{len(required_skills)} skills")
    
    # CHANGED: Accept if ANY skill matches
    if required_matched < min_req_skills:
        filtered_count['min_skills'] += 1
        return None
    
    # REMOVED: Skill relevance threshold - accept all candidates with any match
    candidate_designation = str(row.get('designation', ''))
    matched_skills = list(matched_details.keys())
    skill_relevance_score = calculate_skill_relevance_score(
        matched_skills, jd_role_title, candidate_designation
    )
    
    # Calculate scores
    candidate_scores = calculate_candidate_scores(
        matched_details, required_skills, required_matched, 
        total_weighted_score, total_weight, skill_relevance_score,
        jd_role_title, candidate_designation, row, location_preference,
        required_experience, priority_skills, nice_to_have_matched, nice_to_have_skills
    )
    
    # CHANGED: Ultra low quality threshold of 5
    effective_quality_threshold = max(min_quality_threshold, 5)
    
    if candidate_scores['quality_score'] < effective_quality_threshold:
        filtered_count['quality'] += 1
        return None
    
    # CHANGED: Ultra low match percentage threshold
    if candidate_scores['combined_score'] < min_match_percentage:
        filtered_count['threshold'] += 1
        return None
    
    # Build final candidate data
    return build_candidate_data(row, candidate_scores, matched_details, matched_skills, 
                               required_skills, priority_skills, nice_to_have_matched)

def is_candidate_designation_relevant(jd_role_title, candidate_designation):
    '''ULTRA LENIENT: Always return True - accept all designations'''
    return True

def parse_candidate_skills(skills_str):
    '''Parse and filter candidate skills - MORE LENIENT'''
    candidate_skills_raw = re.split(r'[,;|\n]', skills_str)
    candidate_skills = [normalize_skill(s) for s in candidate_skills_raw if s.strip()]
    
    # CHANGED: More lenient filtering - only remove very short skills
    return [
        s for s in candidate_skills 
        if len(s) > 2  # CHANGED: Accept 3+ character skills (was 4+)
    ]

def match_skills(candidate_skills, required_skills, priority_skills, nice_to_have_skills, jd_role_title):
    '''
    Match candidate skills with SMART MATCHING
    - Accepts scores >= 55 (partial matches)
    - Uses token-based matching for multi-word skills
    '''
    matched_details = {}
    total_weighted_score = 0
    total_weight = 0
    
    # Normalize candidate skills once
    candidate_skills_normalized = [normalize_skill(s) for s in candidate_skills]
    
    print(f"🔍 Matching {len(required_skills)} JD skills against {len(candidate_skills_normalized)} candidate skills")
    
    # LOWERED THRESHOLD: Accept scores >= 55 (was 100 in exact matching)
    MATCH_THRESHOLD = 55
    
    # ========================================================================
    # 1. MATCH REQUIRED SKILLS (Weight: 1.0)
    # ========================================================================
    print(f"\n📋 Checking {len(required_skills)} required skills...")
    
    for req_skill in required_skills:
        best_score = 0
        best_match = None
        best_cand_skill = None
        
        # Find best match among all candidate skills
        for i, cand_skill in enumerate(candidate_skills):
            score, match_type, details = calculate_skill_similarity(req_skill, cand_skill, True)
            
            if score > best_score:
                best_score = score
                best_match = {'type': match_type, 'details': details}
                best_cand_skill = cand_skill
        
        if best_score >= MATCH_THRESHOLD:
            matched_details[req_skill] = {
                'score': best_score,
                'weight': 1.0,
                'category': 'required',
                'cand_skill': best_cand_skill,
                **best_match
            }
            total_weighted_score += best_score * 1.0
            
            # Show match type
            if best_score == 100:
                print(f"   ✅ {req_skill} → {best_cand_skill}")
            elif best_score >= 80:
                print(f"   ⚡ {req_skill} ≈ {best_cand_skill} ({best_score})")
            else:
                print(f"   💡 {req_skill} ~ {best_cand_skill} ({best_score})")
        else:
            print(f"   ❌ {req_skill}")
        
        total_weight += 1.0
    
    # ========================================================================
    # 2. MATCH PRIORITY SKILLS (Weight: 2.0)
    # ========================================================================
    if priority_skills:
        print(f"\n⭐ Checking {len(priority_skills)} priority skills...")
        
        for pri_skill in priority_skills:
            # Upgrade if already matched
            if pri_skill in matched_details:
                matched_details[pri_skill]['weight'] = 2.0
                matched_details[pri_skill]['category'] = 'priority'
                total_weighted_score += matched_details[pri_skill]['score'] * 1.0
                total_weight += 1.0
                print(f"   ⭐ Upgraded: {pri_skill}")
                continue
            
            # Find best match
            best_score = 0
            best_match = None
            best_cand_skill = None
            
            for cand_skill in candidate_skills:
                score, match_type, details = calculate_skill_similarity(pri_skill, cand_skill, True)
                if score > best_score:
                    best_score = score
                    best_match = {'type': match_type, 'details': details}
                    best_cand_skill = cand_skill
            
            if best_score >= MATCH_THRESHOLD:
                matched_details[pri_skill] = {
                    'score': best_score,
                    'weight': 2.0,
                    'category': 'priority',
                    'cand_skill': best_cand_skill,
                    **best_match
                }
                total_weighted_score += best_score * 2.0
                total_weight += 2.0
                print(f"   ✅ Priority: {pri_skill} → {best_cand_skill} ({best_score})")
            else:
                total_weight += 2.0
                print(f"   ❌ Priority: {pri_skill}")
    
    # ========================================================================
    # 3. MATCH NICE-TO-HAVE SKILLS (Weight: 0.5)
    # ========================================================================
    nice_to_have_matched = 0
    
    if nice_to_have_skills:
        print(f"\n💡 Checking {len(nice_to_have_skills)} nice-to-have skills...")
        
        for nice_skill in nice_to_have_skills:
            if nice_skill in matched_details:
                continue
            
            best_score = 0
            best_match = None
            best_cand_skill = None
            
            for cand_skill in candidate_skills:
                score, match_type, details = calculate_skill_similarity(nice_skill, cand_skill, True)
                if score > best_score:
                    best_score = score
                    best_match = {'type': match_type, 'details': details}
                    best_cand_skill = cand_skill
            
            if best_score >= MATCH_THRESHOLD:
                matched_details[nice_skill] = {
                    'score': best_score,
                    'weight': 0.5,
                    'category': 'nice_to_have',
                    'cand_skill': best_cand_skill,
                    **best_match
                }
                nice_to_have_matched += 1
                total_weighted_score += best_score * 0.5
                print(f"   ✅ Bonus: {nice_skill} → {best_cand_skill}")
    
    # ========================================================================
    # SUMMARY
    # ========================================================================
    required_matched = sum(1 for d in matched_details.values() if d['category'] in ['required', 'priority'])
    
    print(f"\n📊 Match Summary:")
    print(f"   Required/Priority: {required_matched}/{len(required_skills) + len(priority_skills or [])}")
    print(f"   Nice-to-have: {nice_to_have_matched}/{len(nice_to_have_skills or [])}")
    print(f"   Total matched: {len(matched_details)}")
    print(f"   Weighted score: {total_weighted_score:.1f}/{total_weight:.1f}")
    
    return matched_details, total_weighted_score, total_weight, nice_to_have_matched

def find_best_skill_match(target_skill, candidate_skills, jd_role_title):
    '''Find exact match only'''
    target_norm = normalize_skill(target_skill)
    
    for cand_skill in candidate_skills:
        cand_norm = normalize_skill(cand_skill)
        
        if target_norm == cand_norm:
            return 100, {'type': 'exact', 'cand_skill': cand_skill, 'details': {}}
        
        # Optional: substring match
        if target_norm in cand_norm or cand_norm in target_norm:
            return 85, {'type': 'substring', 'cand_skill': cand_skill, 'details': {}}
    
    return 0, None

def calculate_candidate_scores(matched_details, required_skills, required_matched, 
                              total_weighted_score, total_weight, skill_relevance_score,
                              jd_role_title, candidate_designation, row, location_preference,
                              required_experience, priority_skills, nice_to_have_matched, nice_to_have_skills):
    '''Calculate all scores for a candidate'''
    base_match_pct = (required_matched / len(required_skills)) * 100 if required_skills else 0
    quality_score = (total_weighted_score / total_weight) if total_weight > 0 else 0
    
    # CHANGED: More lenient relevance multiplier (minimum 0.85 instead of 0.7)
    relevance_multiplier = max(0.85, skill_relevance_score / 100)
    quality_score = quality_score * relevance_multiplier
    
    # Combined score
    combined_score = (base_match_pct * 0.6) + (quality_score * 0.4)
    
    # Apply bonuses
    bonuses = calculate_bonuses(
        jd_role_title, candidate_designation, skill_relevance_score,
        row, location_preference, required_experience, priority_skills,
        nice_to_have_matched, nice_to_have_skills
    )
    
    combined_score += sum(bonuses.values())
    combined_score = min(combined_score, 100)
    
    return {
        'base_match_pct': base_match_pct,
        'quality_score': quality_score,
        'combined_score': combined_score,
        'skill_relevance_score': skill_relevance_score,
        'bonuses': bonuses
    }

def calculate_bonuses(jd_role_title, candidate_designation, skill_relevance_score,
                     row, location_preference, required_experience, priority_skills,
                     nice_to_have_matched, nice_to_have_skills):
    '''Calculate all bonus points for a candidate'''
    bonuses = {}
    
    # Designation bonus
    if jd_role_title:
        designation_score, _, designation_details = calculate_designation_similarity(
            jd_role_title, candidate_designation, True
        )
        if designation_score > 0:
            desg_bonus = (designation_score / 100) * 15
            bonuses['designation'] = round(desg_bonus, 1)
    
    # CHANGED: Lower threshold for skill relevance bonus (50 instead of 70)
    if skill_relevance_score >= 50:
        relevance_bonus = ((skill_relevance_score - 50) / 50) * 10
        bonuses['skill_relevance'] = round(relevance_bonus, 1)
    
    # Priority skills bonus
    if priority_skills:
        priority_matched = sum(1 for s in priority_skills if s in [k for k in row.keys()])
        priority_pct = (priority_matched / len(priority_skills))
        priority_bonus = priority_pct * 15
        bonuses['priority'] = round(priority_bonus, 1)
    
    # Nice-to-have bonus
    if nice_to_have_skills:
        nice_bonus = (nice_to_have_matched / len(nice_to_have_skills)) * 5
        bonuses['nice_to_have'] = round(nice_bonus, 1)
    
    # Experience bonus
    if required_experience:
        exp_bonus = calculate_experience_score(row.get('experience'), required_experience) * (10/15)
        if exp_bonus > 0:
            bonuses['experience'] = round(exp_bonus, 1)
    
    # Location bonus
    if location_preference:
        locations = location_preference if isinstance(location_preference, list) else [location_preference]
        cand_location = str(row.get('location', '')).lower()
        for loc in locations:
            if loc.lower() in cand_location:
                bonuses['location'] = 5
                break
    
    return bonuses

def build_candidate_data(row, scores, matched_details, matched_skills, 
                        required_skills, priority_skills, nice_to_have_matched):
    '''Build the final candidate data dictionary'''
    exact_matches = sum(1 for d in matched_details.values() if d['type'] == 'exact')
    priority_matches = sum(1 for s in priority_skills if s in matched_details)
    skill_scores = [d['score'] for d in matched_details.values()]
    avg_skill_strength = np.mean(skill_scores) if skill_scores else 0
    
    designation_score, designation_match_type, designation_details = calculate_designation_similarity(
        scores.get('jd_role_title', ''), row.get('designation', ''), True
    )
    
    return {
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
        'match_percentage': round(scores['combined_score'], 1),
        'quality_score': round(scores['quality_score'], 1),
        'base_match_percentage': round(scores['base_match_pct'], 1),
        'avg_skill_strength': round(avg_skill_strength, 1),
        'skill_relevance_score': round(scores['skill_relevance_score'], 1),
        'designation_match_score': round(designation_score, 1),
        'designation_match_type': designation_match_type,
        'designation_reasoning': designation_details.get('reasoning', 'N/A'),
        'designation_confidence': designation_details.get('confidence', 'N/A'),
        'seniority_match': designation_details.get('seniority_match', False),
        'function_match': designation_details.get('function_match', False),
        'role_equivalent': designation_details.get('role_equivalent', False),
        'matched_skills': matched_skills,
        'matched_skills_count': len(matched_skills),
        'total_required_skills': len(required_skills),
        'exact_matches': exact_matches,
        'priority_matches': priority_matches,
        'nice_to_have_matches': nice_to_have_matched,
        'skill_match_details': matched_details,
        'bonuses': scores['bonuses'],
        'total_bonus': round(sum(scores['bonuses'].values()), 1)
    }

def finalize_results(matched_candidates, filtered_count, total_candidates, min_match_percentage, min_req_skills):
    '''Sort results and display summary'''
    # Sort candidates
    matched_candidates.sort(
        key=lambda x: (
            x['match_percentage'],
            x['matched_skills_count'],
            x['skill_relevance_score'],
            x['designation_match_score'],
            x['quality_score']
        ),
        reverse=True
    )
    
    # Print summary
    print(f"\n📊 Filtering Summary:")
    print(f"   Total candidates: {total_candidates}")
    for filter_type, count in filtered_count.items():
        print(f"   Filtered by {filter_type}: {count}")
    print(f"\n✅ Found {len(matched_candidates)} matching candidates (Ultra-Lenient Mode)")
    
    if matched_candidates:
        print(f"\n🏆 Top 10 Candidates:")
        for i, c in enumerate(matched_candidates[:10], 1):
            print(f"\n{i}. {c['name']} - {c['designation']}")
            print(f"   📧 {c['email']}")
            print(f"   📍 {c['location']} | 💼 {c['experience']}")
            print(f"   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            print(f"   Overall Match: {c['match_percentage']:.1f}%")
            print(f"   Matched Skills: {c['matched_skills_count']}/{c['total_required_skills']}")
            print(f"   Skills: {', '.join(c['matched_skills'][:8])}")
            if len(c['matched_skills']) > 8:
                print(f"          ... and {len(c['matched_skills']) - 8} more")
    else:
        print(f"\n⚠️ No candidates found with even 1 matching skill.")
    
    return matched_candidates

def debug_skill_matching(required_skills, candidate_skills_sample):
    '''Debug helper to see how skills are being normalized and matched'''
    print("\n🔍 SKILL MATCHING DEBUG")
    print("=" * 60)
    
    print("\n📋 Required Skills (after normalization):")
    for skill in required_skills[:10]:
        normalized = normalize_skill(skill)
        print(f"   '{skill}' → '{normalized}'")
    
    print("\n📋 Sample Candidate Skills (after normalization):")
    for skill in candidate_skills_sample[:10]:
        normalized = normalize_skill(skill)
        print(f"   '{skill}' → '{normalized}'")
    
    print("\n🎯 Testing Matches:")
    for req in required_skills[:5]:
        print(f"\n   Looking for: '{req}'")
        for cand in candidate_skills_sample[:10]:
            score, match_type, details = calculate_skill_similarity(req, cand)
            if score > 0:
                print(f"      ✓ Matched '{cand}' (score: {score}, type: {match_type})")
    
    print("\n" + "=" * 60)

# ============================================================================
# SUPPORTING FUNCTIONS (keep as is from original)
# ============================================================================


def normalize_candidate_dataframe(df):
    '''Normalize candidate DataFrame columns to standard format'''
    print(f"📋 Original columns: {df.columns.tolist()}")
    
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
    
    rename_dict = {old: new for old, new in column_mapping.items() if old in df.columns}
    if rename_dict:
        df = df.rename(columns=rename_dict)
        print(f"✅ Renamed columns: {rename_dict}")
    
    standard_columns = [
        'id', 'name', 'email', 'contact', 'location', 
        'linkedin', 'experience', 'skills', 'qualification', 
        'designation', 'cv_link'
    ]
    
    for col in standard_columns:
        if col not in df.columns:
            df[col] = 'N/A'
            print(f"⚠️ Added missing column: {col}")
    
    if 'current_company' not in df.columns:
        df['current_company'] = 'N/A'
    if 'status' not in df.columns:
        df['status'] = 'Active'
    
    print(f"✅ Final columns: {df.columns.tolist()}")
    
    if not df.empty:
        print(f"📊 Sample data (first row):")
        sample = df.iloc[0]
        print(f"   - Name: {sample.get('name', 'N/A')}")
        print(f"   - Email: {sample.get('email', 'N/A')}")
        print(f"   - Skills: {sample.get('skills', 'N/A')[:100]}...")
    
    return df

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

def extract_skills_from_jd(jd_text, domain_hint=""):
    '''Extract ALL skills comprehensively from job description using OpenAI API'''
    # Keep original implementation exactly as is
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

# All other functions remain exactly as in the original code...