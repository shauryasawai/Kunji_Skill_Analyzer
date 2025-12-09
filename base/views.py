from django.http import FileResponse, Http404, HttpResponseForbidden
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.csrf import csrf_protect
from django.core.exceptions import PermissionDenied
from django.utils.decorators import method_decorator
from django.db.models import Q
from .forms import JDUploadForm, CandidateMatchForm
from .models import JobDescription
from .utils import (cleanup_old_matched_files, extract_text_from_file, extract_skills_from_jd, save_jd_to_excel, 
                    generate_linkedin_search_strings, match_candidates_with_jd,
                    export_matched_candidates, fetch_candidates_from_api,fetch_candidates_from_api_initial)
from datetime import datetime
from django.conf import settings
from django.utils import timezone
import json
import os
from pathlib import Path
import logging
import base64
# Configure logging
logger = logging.getLogger(__name__)

# Constants
ALLOWED_FILE_EXTENSIONS = ['.pdf', '.docx', '.doc', '.txt']
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_SESSION_CANDIDATES = 100

def validate_file_upload(uploaded_file):
    """Validate uploaded file for security"""
    # Check file extension
    ext = os.path.splitext(uploaded_file.name)[1].lower()
    if ext not in ALLOWED_FILE_EXTENSIONS:
        return False, f"File type not allowed. Allowed types: {', '.join(ALLOWED_FILE_EXTENSIONS)}"
    
    # Check file size
    if uploaded_file.size > MAX_FILE_SIZE:
        return False, f"File size exceeds maximum allowed size of {MAX_FILE_SIZE / (1024*1024)}MB"
    
    return True, None

def check_object_permission(request, obj):
    """Check if user has permission to access object"""
    if hasattr(obj, 'created_by'):
        if obj.created_by != request.user and not request.user.is_staff:
            return False
    return True

@login_required
@csrf_protect
@require_http_methods(["GET", "POST"])
def upload_jd(request):
    """Upload and analyze job description - requires authentication"""
    if request.method == 'POST':
        form = JDUploadForm(request.POST, request.FILES)
        
        if form.is_valid():
            # Validate uploaded file
            uploaded_file = request.FILES.get('file')
            is_valid, error_msg = validate_file_upload(uploaded_file)
            
            if not is_valid:
                messages.error(request, error_msg)
                logger.warning(f"Invalid file upload attempt by user {request.user.id}: {error_msg}")
                return redirect('upload_jd')
            
            jd = form.save(commit=False)
            jd.created_by = request.user
            jd.file = request.FILES['file']  # Associate with user
            jd.save()
            
            domain = request.POST.get('domain', '')
            
            # Extract text from uploaded file
            file_path = jd.file.path
            
            try:
                jd_text = extract_text_from_file(file_path)
                
                if not jd_text:
                    messages.error(request, "Could not extract text from the file.")
                    # Delete the uploaded file
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    jd.delete()
                    logger.error(f"Text extraction failed for JD {jd.id} by user {request.user.id}")
                    return redirect('upload_jd')
                
                # Store extracted text in database
                jd.jd_text = jd_text
                
                # Extract comprehensive skills using OpenAI
                result = extract_skills_from_jd(jd_text, domain)
                
                # Get LinkedIn optimized skills
                linkedin_skills = result.get('linkedin_optimized_skills', result.get('all_skills', [])[:10])
                
                # Generate LinkedIn search strings
                search_strings = generate_linkedin_search_strings(
                    linkedin_skills,
                    jd.title,
                    result.get('experience_level', 'Mid Level')
                )
                
                # Update model with comprehensive data
                jd.all_skills = ", ".join(result.get('all_skills', []))
                jd.linkedin_skills_string = ", ".join(linkedin_skills)
                jd.linkedin_search_string = json.dumps(search_strings)
                jd.skill_categories = result.get('skill_categories', {})
                jd.role_category = result.get('role_category', 'Unknown')
                jd.experience_level = result.get('experience_level', 'Unknown')
                jd.key_responsibilities = " | ".join(result.get('key_responsibilities', []))
                jd.qualifications = " | ".join(result.get('qualifications', [])) if isinstance(result.get('qualifications'), list) else result.get('qualifications', '')
                jd.save()  # This will trigger file deletion via model's save() method
                
                # Save to Excel with comprehensive data
                excel_data = {
                    'Job Title': jd.title,
                    'All Skills Required': jd.all_skills,
                    'LinkedIn Search Skills': jd.linkedin_skills_string,
                    'LinkedIn Boolean Search': search_strings.get('basic_and', ''),
                    'Role Category': jd.role_category,
                    'Experience Level': jd.experience_level,
                    'Key Responsibilities': jd.key_responsibilities,
                    'Qualifications': jd.qualifications,
                    'Date Uploaded': datetime.now().strftime('%Y-%m-%d'),
                    'Uploaded By': request.user.username
                }
                save_jd_to_excel(excel_data)
                
                logger.info(f"JD {jd.id} successfully analyzed by user {request.user.id}")
                messages.success(request, "Job Description analyzed successfully! Original file deleted for security.")
                return redirect('results', pk=jd.pk)
                
            except Exception as e:
                logger.error(f"Error processing JD upload by user {request.user.id}: {str(e)}")
                messages.error(request, "An error occurred while processing the file. Please try again.")
                if os.path.exists(file_path):
                    os.remove(file_path)
                jd.delete()
                return redirect('upload_jd')
    else:
        form = JDUploadForm()
    
    # Show only user's JDs (staff can see all)
    if request.user.is_staff:
        recent_jds = JobDescription.objects.all()[:10]
    else:
        recent_jds = JobDescription.objects.filter(created_by=request.user)[:10]
    
    return render(request, 'base/upload.html', {'form': form, 'recent_jds': recent_jds})

@login_required
@require_http_methods(["GET"])
def results(request, pk):
    """View job description results - requires authentication and ownership"""
    jd = get_object_or_404(JobDescription, pk=pk)
    
    # Check permission
    if not check_object_permission(request, jd):
        logger.warning(f"Unauthorized access attempt to JD {pk} by user {request.user.id}")
        raise PermissionDenied("You don't have permission to view this job description.")
    
    # Parse LinkedIn search strings
    linkedin_searches = {}
    if jd.linkedin_search_string:
        try:
            linkedin_searches = json.loads(jd.linkedin_search_string)
        except json.JSONDecodeError:
            linkedin_searches = {}
            logger.error(f"Failed to parse LinkedIn search strings for JD {pk}")
    
    # Check API availability
    api_available = hasattr(settings, 'CANDIDATES_API_URL') and settings.CANDIDATES_API_URL
    
    # Get candidate count from API (optional - for display purposes)
    total_candidates = 0
    if api_available:
        try:
            df = fetch_candidates_from_api_initial()
            total_candidates = len(df) if not df.empty else 0
        except Exception as e:
            logger.warning(f"Failed to fetch candidate count: {str(e)}")
    
    match_form = CandidateMatchForm()
    
    context = {
        'jd': jd,
        'all_skills': jd.get_all_skills_list(),
        'linkedin_skills': jd.get_linkedin_skills_list(),
        'linkedin_searches': linkedin_searches,
        'skill_categories': jd.skill_categories,
        'responsibilities': jd.get_responsibilities_list(),
        'qualifications': jd.get_qualifications_list(),
        'match_form': match_form,
        'api_available': api_available,
        'total_candidates': total_candidates,
    }
    
    return render(request, 'base/results.html', context)

@login_required
@require_POST
@csrf_protect
def match_candidates(request, jd_pk):
    """Match candidates from API with JD requirements"""
    jd = get_object_or_404(JobDescription, pk=jd_pk)
    
    # Check permission
    if not check_object_permission(request, jd):
        logger.warning(f"Unauthorized match attempt for JD {jd_pk} by user {request.user.id}")
        raise PermissionDenied("You don't have permission to match candidates for this job description.")
    
    form = CandidateMatchForm(request.POST)
    
    if not form.is_valid():
        logger.warning(f"Invalid form submission for JD {jd_pk}: {form.errors}")
        messages.error(request, "Invalid form data. Please check your inputs.")
        return redirect('results', pk=jd.pk)
    
    # Get form data
    min_match = form.cleaned_data['min_match_percentage']
    
    # Get required skills from JD
    required_skills = jd.get_all_skills_list()
    
    if not required_skills:
        messages.error(request, "No skills found in the job description.")
        return redirect('results', pk=jd.pk)
    
    try:
        logger.info(f"Matching candidates for JD {jd_pk} with {len(required_skills)} skills, min_match={min_match}%")
        
        # Match candidates from API
        matched_candidates = match_candidates_with_jd(
            required_skills=required_skills,
            min_match_percentage=min_match
        )
        
        if not matched_candidates:
            logger.info(f"No matches found for JD {jd_pk} with threshold {min_match}%")
            messages.warning(request, "No candidates found matching the criteria. Try lowering the match percentage.")
            return redirect('results', pk=jd.pk)
        
        # Export to Excel
        output_filename = f"matched_candidates_{jd.title.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        output_path = Path(settings.MEDIA_ROOT) / 'matched_candidates' / output_filename
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        if not export_matched_candidates(matched_candidates, output_path):
            messages.error(request, "Failed to export matched candidates.")
            return redirect('results', pk=jd.pk)
        
        # Store in session (limit data for performance)
        session_candidates = []
        for candidate in matched_candidates[:MAX_SESSION_CANDIDATES]:
            session_candidates.append({
                'id': candidate.get('id', 'N/A'),
                'name': candidate['name'],
                'email': candidate['email'],
                'contact': candidate['contact'],
                'designation': candidate['designation'],
                'current_company': candidate.get('current_company', 'N/A'),
                'experience': candidate['experience'],
                'location': candidate['location'],
                'linkedin': candidate['linkedin'],
                'qualification': candidate.get('qualification', 'N/A'),
                'match_percentage': candidate['match_percentage'],
                'matched_skills_count': candidate['matched_skills_count'],
                'total_required_skills': candidate['total_required_skills'],
                'matched_skills': candidate['matched_skills'][:15],
                'cv_link': candidate.get('cv_link', 'N/A'),
                'status': candidate.get('status', 'Active')
            })
        
        # Store session data
        request.session['matched_candidates'] = session_candidates
        request.session['output_file'] = str(output_path.relative_to(settings.MEDIA_ROOT))
        request.session['total_matches'] = len(matched_candidates)
        request.session['jd_id'] = jd.pk
        
        # Cleanup old files
        cleanup_old_matched_files(days=1)
        
        logger.info(f"Found {len(matched_candidates)} matches for JD {jd_pk}")
        messages.success(request, f"Found {len(matched_candidates)} matching candidates!")
        return redirect('show_matches', jd_pk=jd.pk)
        
    except Exception as e:
        logger.error(f"Error matching candidates for JD {jd_pk}: {str(e)}")
        messages.error(request, f"An error occurred while matching candidates: {str(e)}")
        return redirect('results', pk=jd.pk)

@login_required
@require_http_methods(["GET"])
def show_matches(request, jd_pk):
    """Display matched candidates - requires authentication and ownership"""
    jd = get_object_or_404(JobDescription, pk=jd_pk)
    
    # Check permission
    if not check_object_permission(request, jd):
        logger.warning(f"Unauthorized access to matches for JD {jd_pk} by user {request.user.id}")
        raise PermissionDenied("You don't have permission to view these matches.")
    
    # Verify session data belongs to this JD
    session_jd_id = request.session.get('jd_id')
    if session_jd_id != jd_pk:
        logger.warning(f"Session JD mismatch for user {request.user.id}")
        messages.error(request, "Invalid session data. Please run the match again.")
        return redirect('results', pk=jd_pk)
    
    matched_candidates = request.session.get('matched_candidates', [])
    output_file = request.session.get('output_file', '')
    total_matches = request.session.get('total_matches', len(matched_candidates))
    match_settings = request.session.get('match_settings', {})
    
    # Calculate statistics
    if matched_candidates:
        avg_match = sum(c['match_percentage'] for c in matched_candidates) / len(matched_candidates)
        top_match = max(matched_candidates, key=lambda x: x['match_percentage'])
    else:
        avg_match = 0
        top_match = None
    
    context = {
        'jd': jd,
        'matched_candidates': matched_candidates,
        'output_file': output_file,
        'total_matches': total_matches,
        'match_settings': match_settings,
        'avg_match_percentage': round(avg_match, 1),
        'top_match': top_match,
        'displayed_count': len(matched_candidates),
        'has_more': total_matches > len(matched_candidates),
    }
    
    return render(request, 'base/show_matches.html', context)

from io import BytesIO
@login_required
@require_http_methods(["GET"])
def download_matched_file(request, jd_pk):
    """Download matched candidates file - Vercel compatible (session-based storage)"""
    jd = get_object_or_404(JobDescription, pk=jd_pk)
    
    # Check permission
    if not check_object_permission(request, jd):
        logger.warning(f"Unauthorized download attempt for JD {jd_pk} by user {request.user.id}")
        raise PermissionDenied("You don't have permission to download this file.")
    
    # Verify session data belongs to this JD
    session_jd_id = request.session.get('jd_id')
    if session_jd_id != jd_pk:
        logger.warning(f"Session JD mismatch for download by user {request.user.id}")
        raise Http404("File not found or session expired")
    
    # Get file data from session (base64 encoded)
    file_data_b64 = request.session.get('excel_file_data')
    filename = request.session.get('excel_filename', 'matched_candidates.xlsx')
    
    if not file_data_b64:
        logger.warning(f"No file data in session for JD {jd_pk}")
        raise Http404("File not found or session expired")
    
    try:
        # Decode base64 file data
        file_data = base64.b64decode(file_data_b64)
        
        # Serve from memory
        response = FileResponse(
            BytesIO(file_data),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        
        # Clear session data after successful download
        request.session.pop('matched_candidates', None)
        request.session.pop('excel_file_data', None)
        request.session.pop('excel_filename', None)
        request.session.pop('output_file', None)
        request.session.pop('jd_id', None)
        request.session.pop('match_settings', None)
        
        logger.info(f"File downloaded successfully by user {request.user.id} for JD {jd_pk}")
        return response
    
    except base64.binascii.Error as e:
        logger.error(f"Base64 decode error for user {request.user.id}: {str(e)}")
        messages.error(request, "File data is corrupted. Please regenerate the matches.")
        return redirect('show_matches', jd_pk=jd_pk)
    except Exception as e:
        logger.error(f"Download error for user {request.user.id}: {str(e)}")
        messages.error(request, f"Error downloading file: {str(e)}")
        return redirect('show_matches', jd_pk=jd_pk)

@login_required
@require_http_methods(["GET"])
def test_api_connection(request):
    """Test API connection and display candidate count"""
    try:
        df = fetch_candidates_from_api()
        
        if df.empty:
            messages.warning(request, "API connection successful but no candidates found.")
        else:
            messages.success(request, f"API connection successful! Found {len(df)} candidates.")
            
            # Show sample columns
            if not df.empty:
                columns = df.columns.tolist()
                messages.info(request, f"Available columns: {', '.join(columns[:10])}")
        
        return redirect('upload_jd')
        
    except Exception as e:
        logger.error(f"API connection test failed: {str(e)}")
        messages.error(request, f"API connection failed: {str(e)}")
        return redirect('upload_jd')
    

@login_required
@require_http_methods(["GET"])
def test_api_connection_init(request):
    """Test API connection and display candidate count"""
    try:
        df = fetch_candidates_from_api_initial()
        
        if df.empty:
            messages.warning(request, "API connection successful but no candidates found.")
        else:
            messages.success(request, f"API connection successful! Found {len(df)} candidates.")
            
            # Show sample columns
            if not df.empty:
                columns = df.columns.tolist()
                messages.info(request, f"Available columns: {', '.join(columns[:10])}")
        
        return redirect('upload_jd')
        
    except Exception as e:
        logger.error(f"API connection test failed: {str(e)}")
        messages.error(request, f"API connection failed: {str(e)}")
        return redirect('upload_jd')