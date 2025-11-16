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

2. "skill_categories": Organize the skills into categories like:
   {{"Technical": [...], "Tools": [...], "Soft Skills": [...], "Domain Knowledge": [...], "Certifications": [...]}}
   
3. "linkedin_optimized_skills": A list of 8-15 MOST IMPORTANT skills optimized for LinkedIn Recruiter search. 
   - Focus on searchable, industry-standard terms
   - Remove generic terms like "communication" or "teamwork"
   - Prioritize: specific technologies, tools, certifications, frameworks
   - Use exact names as they appear on LinkedIn (e.g., "JavaScript" not "JS", "Amazon Web Services (AWS)" not just "AWS")

4. "role_category": The most suitable role category (e.g., HR, Marketing, IT, Finance, Sales, Operations, etc.)

5. "experience_level": one of ["Entry Level", "Mid Level", "Senior Level", "Executive Level"]

6. "key_responsibilities": List 5-7 main responsibilities mentioned in the JD

7. "qualifications": Educational requirements and certifications

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
            
            if "linkedin_optimized_skills" not in result:
                result["linkedin_optimized_skills"] = result.get("all_skills", [])[:10]
                
            if "all_skills" not in result:
                result["all_skills"] = []
            
            if "skill_categories" not in result:
                result["skill_categories"] = {}
                
            if "role_category" not in result:
                result["role_category"] = "Unknown"
                
            if "experience_level" not in result:
                result["experience_level"] = "Unknown"
                
            if "key_responsibilities" not in result:
                result["key_responsibilities"] = []
                
            if "qualifications" not in result:
                result["qualifications"] = []
            
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
        "skill_categories": {},
        "role_category": "Unknown",
        "experience_level": "Unknown",
        "key_responsibilities": [],
        "qualifications": []
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


def fetch_candidates_from_api(api_url=None, api_key=None, timeout=30):
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

def match_candidates_with_jd(required_skills, min_match_percentage=50, api_url=None, api_key=None):
    '''
    Match candidates from API with job requirements
    
    Args:
        required_skills: List of skills from JD
        min_match_percentage: Minimum percentage of skills that must match (default 50%)
        api_url: Optional API URL (uses settings if not provided)
        api_key: Optional API key (uses settings if not provided)
    
    Returns:
        List of matched candidates with match scores
    '''
    try:
        # Fetch candidates from API
        df = fetch_candidates_from_api(api_url, api_key)
        
        if df.empty:
            print("❌ No candidates found from API")
            return []
        
        # Check if skills column exists
        if 'skills' not in df.columns:
            print("❌ Error: 'skills' column not found in API response")
            print(f"Available columns: {df.columns.tolist()}")
            return []
        
        print(f"📊 Processing {len(df)} candidates from API")
        
        matched_candidates = []
        
        # Normalize required skills for comparison
        required_skills_lower = [skill.lower().strip() for skill in required_skills if skill.strip()]
        
        if not required_skills_lower:
            print("❌ No valid required skills provided")
            return []
        
        print(f"🎯 Required skills: {required_skills_lower}")
        
        for idx, row in df.iterrows():
            candidate_skills_str = str(row.get('skills', ''))
            
            # Skip candidates with no skills
            if not candidate_skills_str or candidate_skills_str.lower() in ['nan', 'none', '']:
                continue
            
            # Parse candidate skills (comma, semicolon, or pipe separated)
            candidate_skills_raw = re.split(r'[,;|]', candidate_skills_str)
            candidate_skills = [s.lower().strip() for s in candidate_skills_raw if s.strip()]
            
            if not candidate_skills:
                continue
            
            # Calculate skill matches with flexible matching
            matched_skills = []
            matched_skill_names = set()  # Track unique matches
            
            for req_skill in required_skills_lower:
                req_skill_words = set(req_skill.split())
                
                for cand_skill in candidate_skills:
                    cand_skill_words = set(cand_skill.split())
                    
                    # Method 1: Exact match
                    if req_skill == cand_skill:
                        if req_skill not in matched_skill_names:
                            matched_skills.append(req_skill)
                            matched_skill_names.add(req_skill)
                        break
                    
                    # Method 2: Substring match (either direction)
                    elif req_skill in cand_skill or cand_skill in req_skill:
                        if req_skill not in matched_skill_names:
                            matched_skills.append(req_skill)
                            matched_skill_names.add(req_skill)
                        break
                    
                    # Method 3: Word overlap (at least one common word)
                    elif req_skill_words & cand_skill_words:
                        # Check if there's meaningful overlap (not just common words like "and", "or")
                        common_words = req_skill_words & cand_skill_words
                        if common_words and not all(w in ['and', 'or', 'the', 'a', 'an'] for w in common_words):
                            if req_skill not in matched_skill_names:
                                matched_skills.append(req_skill)
                                matched_skill_names.add(req_skill)
                            break
            
            # Calculate match percentage
            match_percentage = (len(matched_skills) / len(required_skills_lower)) * 100 if required_skills_lower else 0
            
            # Only include candidates above threshold
            if match_percentage >= min_match_percentage:
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
                    'matched_skills': matched_skills,
                    'match_percentage': round(match_percentage, 1),
                    'matched_skills_count': len(matched_skills),
                    'total_required_skills': len(required_skills_lower)
                }
                matched_candidates.append(candidate_data)
                
                # Debug: Print first few matches
                if len(matched_candidates) <= 3:
                    print(f"✅ Match {len(matched_candidates)}: {candidate_data['name']} - {match_percentage}% ({len(matched_skills)}/{len(required_skills_lower)} skills)")
        
        # Sort by match percentage (highest first)
        matched_candidates.sort(key=lambda x: x['match_percentage'], reverse=True)
        
        print(f"✅ Found {len(matched_candidates)} matching candidates (threshold: {min_match_percentage}%)")
        
        if len(matched_candidates) == 0:
            print(f"⚠️ No candidates matched. Try lowering threshold or check if candidate skills format matches expected format.")
            print(f"💡 Sample candidate skills (first 3):")
            for idx, row in df.head(3).iterrows():
                print(f"   - {row.get('name', 'N/A')}: {row.get('skills', 'N/A')}")
        
        return matched_candidates
    
    except Exception as e:
        print(f"❌ Error matching candidates: {e}")
        import traceback
        traceback.print_exc()
        return []

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
            'match_percentage', 'matched_skills_count', 'total_required_skills',
            'name', 'email', 'contact', 'designation', 'current_company',
            'experience', 'location', 'qualification', 'linkedin',
            'skills', 'matched_skills', 'cv_link', 'status', 'id'
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