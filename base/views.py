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
MAX_SESSION_CANDIDATES = 300

# ============================================================
# TOKEN TRACKING UTILITIES
# ============================================================

def log_token_usage(user, operation, prompt_tokens, completion_tokens, total_tokens, model="gpt-4", cost=0.0):
    """
    Log OpenAI token usage to database and logger
    
    Args:
        user: Django User object
        operation: String describing the operation (e.g., "skill_extraction", "matching")
        prompt_tokens: Number of input tokens
        completion_tokens: Number of output tokens
        total_tokens: Total tokens used
        model: OpenAI model name
        cost: Estimated cost in USD
    """
    try:
        # Import here to avoid circular imports
        from .models import TokenUsageLog
        
        TokenUsageLog.objects.create(
            user=user,
            operation=operation,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            model=model,
            cost=cost
        )
        
        logger.info(
            f"Token Usage - User: {user.username}, Operation: {operation}, "
            f"Prompt: {prompt_tokens}, Completion: {completion_tokens}, "
            f"Total: {total_tokens}, Model: {model}, Cost: ${cost:.4f}"
        )
    except Exception as e:
        logger.error(f"Failed to log token usage: {str(e)}")


def calculate_openai_cost(prompt_tokens, completion_tokens, model="gpt-4"):
    """
    Calculate estimated cost based on OpenAI pricing
    Update these prices according to current OpenAI pricing
    
    Pricing as of 2024:
    - GPT-4: $0.03/1K prompt tokens, $0.06/1K completion tokens
    - GPT-3.5-turbo: $0.0015/1K prompt tokens, $0.002/1K completion tokens
    """
    pricing = {
        'gpt-4': {'prompt': 0.03, 'completion': 0.06},
        'gpt-4-turbo': {'prompt': 0.01, 'completion': 0.03},
        'gpt-3.5-turbo': {'prompt': 0.0015, 'completion': 0.002},
    }
    
    model_pricing = pricing.get(model, pricing['gpt-4'])
    
    prompt_cost = (prompt_tokens / 1000) * model_pricing['prompt']
    completion_cost = (completion_tokens / 1000) * model_pricing['completion']
    
    return prompt_cost + completion_cost


def get_user_token_stats(user, days=30):
    """Get token usage statistics for a user"""
    try:
        from .models import TokenUsageLog
        from django.utils import timezone
        from datetime import timedelta
        
        since_date = timezone.now() - timedelta(days=days)
        
        logs = TokenUsageLog.objects.filter(
            user=user,
            created_at__gte=since_date
        )
        
        stats = {
            'total_tokens': sum(log.total_tokens for log in logs),
            'total_cost': sum(log.cost for log in logs),
            'operation_breakdown': {},
            'daily_usage': []
        }
        
        # Group by operation
        for log in logs:
            if log.operation not in stats['operation_breakdown']:
                stats['operation_breakdown'][log.operation] = {
                    'count': 0,
                    'tokens': 0,
                    'cost': 0.0
                }
            stats['operation_breakdown'][log.operation]['count'] += 1
            stats['operation_breakdown'][log.operation]['tokens'] += log.total_tokens
            stats['operation_breakdown'][log.operation]['cost'] += log.cost
        
        return stats
    except Exception as e:
        logger.error(f"Failed to get token stats: {str(e)}")
        return None

# ============================================================
# EXISTING VALIDATION FUNCTIONS
# ============================================================

def validate_file_upload(uploaded_file):
    """Validate uploaded file for security"""
    ext = os.path.splitext(uploaded_file.name)[1].lower()
    if ext not in ALLOWED_FILE_EXTENSIONS:
        return False, f"File type not allowed. Allowed types: {', '.join(ALLOWED_FILE_EXTENSIONS)}"
    
    if uploaded_file.size > MAX_FILE_SIZE:
        return False, f"File size exceeds maximum allowed size of {MAX_FILE_SIZE / (1024*1024)}MB"
    
    return True, None


def check_object_permission(request, obj):
    """Check if user has permission to access object"""
    if hasattr(obj, 'created_by'):
        if obj.created_by != request.user and not request.user.is_staff:
            return False
    return True

# ============================================================
# MODIFIED VIEWS WITH TOKEN TRACKING
# ============================================================

@login_required
@csrf_protect
@require_http_methods(["GET", "POST"])
def upload_jd(request):
    """Upload and analyze job description - requires authentication"""
    if request.method == 'POST':
        form = JDUploadForm(request.POST, request.FILES)
        
        if form.is_valid():
            uploaded_file = request.FILES.get('file')
            is_valid, error_msg = validate_file_upload(uploaded_file)
            
            if not is_valid:
                messages.error(request, error_msg)
                logger.warning(f"Invalid file upload attempt by user {request.user.id}: {error_msg}")
                return redirect('upload_jd')
            
            jd = form.save(commit=False)
            jd.created_by = request.user
            jd.file = request.FILES['file']
            jd.save()
            
            domain = request.POST.get('domain', '')
            file_path = jd.file.path
            
            try:
                jd_text = extract_text_from_file(file_path)
                
                if not jd_text:
                    messages.error(request, "Could not extract text from the file.")
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    jd.delete()
                    logger.error(f"Text extraction failed for JD {jd.id} by user {request.user.id}")
                    return redirect('upload_jd')
                
                jd.jd_text = jd_text
                
                # ============================================================
                # MODIFIED: Extract skills with token tracking
                # ============================================================
                result = extract_skills_from_jd(jd_text, domain)
                
                # Check if token usage was returned
                if 'token_usage' in result:
                    token_usage = result['token_usage']
                    model = result.get('model', 'gpt-4')
                    
                    # Calculate cost
                    cost = calculate_openai_cost(
                        token_usage['prompt_tokens'],
                        token_usage['completion_tokens'],
                        model
                    )
                    
                    # Log token usage
                    log_token_usage(
                        user=request.user,
                        operation='skill_extraction',
                        prompt_tokens=token_usage['prompt_tokens'],
                        completion_tokens=token_usage['completion_tokens'],
                        total_tokens=token_usage['total_tokens'],
                        model=model,
                        cost=cost
                    )
                    
                    # Add user-friendly message
                    messages.info(
                        request,
                        f"AI Analysis: {token_usage['total_tokens']} tokens used (${cost:.4f})"
                    )
                
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
                jd.save()
                
                # Save to Excel
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
    
    # Show user's token usage stats
    token_stats = get_user_token_stats(request.user, days=30)
    
    if request.user.is_staff:
        recent_jds = JobDescription.objects.all()[:10]
    else:
        recent_jds = JobDescription.objects.filter(created_by=request.user)[:10]
    
    context = {
        'form': form,
        'recent_jds': recent_jds,
        'token_stats': token_stats
    }
    
    return render(request, 'base/upload.html', context)


@login_required
@require_http_methods(["GET"])
def results(request, pk):
    """View job description results - requires authentication and ownership"""
    jd = get_object_or_404(JobDescription, pk=pk)
    
    if not check_object_permission(request, jd):
        logger.warning(f"Unauthorized access attempt to JD {pk} by user {request.user.id}")
        raise PermissionDenied("You don't have permission to view this job description.")
    
    linkedin_searches = {}
    if jd.linkedin_search_string:
        try:
            linkedin_searches = json.loads(jd.linkedin_search_string)
        except json.JSONDecodeError:
            linkedin_searches = {}
            logger.error(f"Failed to parse LinkedIn search strings for JD {pk}")
    
    api_available = hasattr(settings, 'CANDIDATES_API_URL') and settings.CANDIDATES_API_URL
    
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
    
    if not check_object_permission(request, jd):
        logger.warning(f"Unauthorized match attempt for JD {jd_pk} by user {request.user.id}")
        raise PermissionDenied("You don't have permission to match candidates for this job description.")
    
    form = CandidateMatchForm(request.POST)
    
    if not form.is_valid():
        logger.warning(f"Invalid form submission for JD {jd_pk}: {form.errors}")
        messages.error(request, "Invalid form data. Please check your inputs.")
        return redirect('results', pk=jd.pk)
    
    min_match = form.cleaned_data['min_match_percentage']
    required_skills = jd.get_all_skills_list()
    
    if not required_skills:
        messages.error(request, "No skills found in the job description.")
        return redirect('results', pk=jd.pk)
    
    try:
        logger.info(f"Matching candidates for JD {jd_pk} with {len(required_skills)} skills, min_match={min_match}%")
        
        result = match_candidates_with_jd(
            required_skills=required_skills,
            min_match_percentage=min_match
        )
        
        # Check if result includes token usage (if matching uses OpenAI)
        if isinstance(result, dict) and 'candidates' in result:
            matched_candidates = result['candidates']
            
            if 'token_usage' in result:
                token_usage = result['token_usage']
                model = result.get('model', 'gpt-4')
                
                cost = calculate_openai_cost(
                    token_usage['prompt_tokens'],
                    token_usage['completion_tokens'],
                    model
                )
                
                log_token_usage(
                    user=request.user,
                    operation='candidate_matching',
                    prompt_tokens=token_usage['prompt_tokens'],
                    completion_tokens=token_usage['completion_tokens'],
                    total_tokens=token_usage['total_tokens'],
                    model=model,
                    cost=cost
                )
        else:
            matched_candidates = result
        
        if not matched_candidates:
            logger.info(f"No matches found for JD {jd_pk} with threshold {min_match}%")
            messages.warning(request, "No candidates found matching the criteria. Try lowering the match percentage.")
            return redirect('results', pk=jd.pk)
        
        success, message = export_matched_candidates(request, matched_candidates, jd_pk)
        
        if not success:
            messages.error(request, message)
            return redirect('results', pk=jd.pk)
        
        # Store limited candidate data in session
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
        
        request.session['matched_candidates'] = session_candidates
        request.session['total_matches'] = len(matched_candidates)
        request.session['jd_id'] = jd.pk
        request.session['match_settings'] = {
            'min_match_percentage': min_match,
            'total_skills': len(required_skills)
        }
        # FIX: Add sheet_name to session (it's set by export_matched_candidates but let's be explicit)
        request.session['sheet_name'] = 'Matched Candidates'
        
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
    
    if not check_object_permission(request, jd):
        logger.warning(f"Unauthorized access to matches for JD {jd_pk} by user {request.user.id}")
        raise PermissionDenied("You don't have permission to view these matches.")
    
    session_jd_id = request.session.get('jd_id')
    if session_jd_id != jd_pk:
        logger.warning(f"Session JD mismatch for user {request.user.id}")
        messages.error(request, "Invalid session data. Please run the match again.")
        return redirect('results', pk=jd_pk)
    
    matched_candidates = request.session.get('matched_candidates', [])
    # FIX: Add this line to get excel file info from session
    output_file = request.session.get('excel_filename', '')
    sheet_name = request.session.get('sheet_name', 'Matched Candidates')
    
    total_matches = request.session.get('total_matches', len(matched_candidates))
    match_settings = request.session.get('match_settings', {})
    
    if matched_candidates:
        avg_match = sum(c['match_percentage'] for c in matched_candidates) / len(matched_candidates)
        top_match = max(matched_candidates, key=lambda x: x['match_percentage'])
    else:
        avg_match = 0
        top_match = None
    
    context = {
        'jd': jd,
        'matched_candidates': matched_candidates,
        # FIX: Add these two lines
        'output_file': output_file,
        'sheet_name': sheet_name,
        'total_matches': total_matches,
        'match_settings': match_settings,
        'avg_match_percentage': round(avg_match, 1),
        'top_match': top_match,
        'displayed_count': len(matched_candidates),
        'has_more': total_matches > len(matched_candidates),
    }
    
    return render(request, 'base/show_matches.html', context)


@login_required
@require_http_methods(["GET"])
def download_matched_file(request, jd_pk):
    """Download matched candidates file - Vercel compatible"""
    from io import BytesIO
    
    jd = get_object_or_404(JobDescription, pk=jd_pk)
    
    if not check_object_permission(request, jd):
        logger.warning(f"Unauthorized download attempt for JD {jd_pk} by user {request.user.id}")
        raise PermissionDenied("You don't have permission to download this file.")
    
    session_jd_id = request.session.get('jd_id')
    if session_jd_id != jd_pk:
        logger.warning(f"Session JD mismatch for download by user {request.user.id}")
        messages.error(request, "File not found or session expired. Please run the match again.")
        return redirect('show_matches', jd_pk=jd_pk)
    
    file_data_b64 = request.session.get('excel_file_data')
    filename = request.session.get('excel_filename', 'matched_candidates.xlsx')
    
    if not file_data_b64:
        logger.warning(f"No file data in session for JD {jd_pk}")
        messages.error(request, "File not found or session expired. Please run the match again.")
        return redirect('show_matches', jd_pk=jd_pk)
    
    try:
        file_data = base64.b64decode(file_data_b64)
        
        response = FileResponse(
            BytesIO(file_data),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        
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
            
            if not df.empty:
                columns = df.columns.tolist()
                messages.info(request, f"Available columns: {', '.join(columns[:10])}")
        
        return redirect('upload_jd')
        
    except Exception as e:
        logger.error(f"API connection test failed: {str(e)}")
        messages.error(request, f"API connection failed: {str(e)}")
        return redirect('upload_jd')


# ============================================================
# Token Usage Dashboard
# ============================================================

from django.core.cache import cache
from django.db.models import Sum, Avg, Count

def get_organization_stats():
    """Get organization-wide token usage statistics"""
    try:
        from .models import TokenUsageLog
        from django.utils import timezone
        from datetime import timedelta
        
        since_date = timezone.now() - timedelta(days=30)
        
        # Use aggregation for better performance
        stats = TokenUsageLog.objects.filter(
            created_at__gte=since_date
        ).aggregate(
            total_tokens=Sum('total_tokens'),
            total_cost=Sum('cost'),
            total_users=Count('user', distinct=True),
            total_operations=Count('id')
        )
        
        return {
            'total_tokens': stats['total_tokens'] or 0,
            'total_cost': stats['total_cost'] or 0.0,
            'total_users': stats['total_users'] or 0,
            'total_operations': stats['total_operations'] or 0
        }
    except Exception as e:
        logger.error(f"Failed to get org stats: {str(e)}")
        return None
    

def calculate_usage_insights(stats_30, stats_7, stats_today):
    """Calculate usage insights and trends"""
    insights = {
        'trend': 'stable',
        'efficiency_score': 0,
        'recommendations': []
    }
    
    try:
        # Calculate trend
        if stats_30['total_tokens'] and stats_7['total_tokens']:
            weekly_rate = stats_7['total_tokens'] / 7
            monthly_rate = stats_30['total_tokens'] / 30
            
            if weekly_rate > monthly_rate * 1.5:
                insights['trend'] = 'increasing'
                insights['recommendations'].append(
                    'Usage is higher than average this week. Consider optimizing prompts.'
                )
            elif weekly_rate < monthly_rate * 0.5:
                insights['trend'] = 'decreasing'
            else:
                insights['trend'] = 'stable'
        
        # Calculate efficiency score (0-100)
        if stats_30['total_tokens'] and stats_30['total_cost']:
            cost_per_1k = (stats_30['total_cost'] / stats_30['total_tokens']) * 1000
            
            # Assuming gpt-4o-mini pricing ($0.15 per 1M tokens input, $0.60 per 1M tokens output)
            # Average should be around $0.0004 per 1K tokens
            if cost_per_1k <= 0.0004:
                insights['efficiency_score'] = 100
            elif cost_per_1k <= 0.0006:
                insights['efficiency_score'] = 80
            elif cost_per_1k <= 0.001:
                insights['efficiency_score'] = 60
            else:
                insights['efficiency_score'] = 40
                insights['recommendations'].append(
                    'Consider using more efficient models or optimizing prompt lengths.'
                )
        
        # Operation-specific insights
        if stats_30.get('operation_breakdown'):
            breakdown = stats_30['operation_breakdown']
            
            # Find most expensive operation
            most_expensive = max(breakdown.items(), key=lambda x: x[1]['cost'])
            insights['most_expensive_operation'] = most_expensive[0]
            
            # Check if skill extraction is too frequent
            if 'skill_extraction' in breakdown:
                skill_count = breakdown['skill_extraction']['count']
                if skill_count > 100:
                    insights['recommendations'].append(
                        f'You performed {skill_count} skill extractions. Consider batching or caching results.'
                    )
        
        # Daily usage pattern
        if stats_today['total_tokens']:
            daily_avg = stats_30['total_tokens'] / 30 if stats_30['total_tokens'] else 0
            if stats_today['total_tokens'] > daily_avg * 2:
                insights['recommendations'].append(
                    'Today\'s usage is unusually high. Monitor your operations.'
                )
    
    except Exception as e:
        logger.error(f"Error calculating insights: {str(e)}")
    
    return insights

@login_required
@require_http_methods(["GET"])
def token_usage_dashboard(request):
    """
    Display comprehensive token usage statistics for the user
    With caching for better performance
    """
    user = request.user
    cache_key = f'token_stats_{user.id}'
    
    # Try to get cached stats (cache for 5 minutes)
    cached_stats = cache.get(cache_key)
    
    if cached_stats and not request.GET.get('refresh'):
        # Use cached data
        context = cached_stats
        context['cached'] = True
        context['cache_time'] = cache.ttl(cache_key)
    else:
        # Calculate fresh stats
        stats_30_days = get_user_token_stats(request.user, days=30)
        stats_7_days = get_user_token_stats(request.user, days=7)
        stats_today = get_user_token_stats(request.user, days=1)
        
        # Get recent token logs with optimized query
        try:
            from .models import TokenUsageLog
            recent_logs = TokenUsageLog.objects.filter(
                user=request.user
            ).select_related('user').order_by('-created_at')[:50]
        except:
            recent_logs = []
        
        # For staff users, show organization-wide stats
        org_stats = None
        if request.user.is_staff:
            org_stats = get_organization_stats()
        
        # Calculate additional insights
        insights = calculate_usage_insights(stats_30_days, stats_7_days, stats_today)
        
        context = {
            'stats_30_days': stats_30_days,
            'stats_7_days': stats_7_days,
            'stats_today': stats_today,
            'recent_logs': recent_logs,
            'org_stats': org_stats,
            'insights': insights,
            'cached': False
        }
        
        # Cache for 5 minutes
        cache.set(cache_key, context, 300)
    
    return render(request, 'base/token_usage_dashboard.html', context)

@login_required
@require_http_methods(["POST"])
def clear_token_cache(request):
    """Clear cached token statistics"""
    cache_key = f'token_stats_{request.user.id}'
    cache.delete(cache_key)
    messages.success(request, "Token statistics refreshed successfully!")
    return redirect('token_usage_dashboard')