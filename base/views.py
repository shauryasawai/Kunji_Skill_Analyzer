from django.http import FileResponse, Http404, HttpResponseForbidden
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.csrf import csrf_protect
from django.core.exceptions import PermissionDenied
from django.db.models import Q, Sum, Avg, Count
from django.core.cache import cache
from django.conf import settings
from django.utils import timezone

from .forms import JDUploadForm, CandidateMatchForm
from .models import JobDescription
from .utils import (
    cleanup_old_matched_files, extract_text_from_file, extract_skills_from_jd,
    save_jd_to_excel, generate_linkedin_search_strings, match_candidates_with_jd,
    export_matched_candidates, fetch_candidates_from_api, fetch_candidates_from_api_initial,
)

from datetime import datetime
import json
import os
import base64
import logging

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────
ALLOWED_FILE_EXTENSIONS = ['.pdf', '.docx', '.doc', '.txt']
MAX_FILE_SIZE = 5 * 1024 * 1024   # 5 MB
MAX_SESSION_CANDIDATES = 300


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def validate_file_upload(uploaded_file):
    """Validate uploaded file — handles None, size, and type."""
    if uploaded_file is None:
        return False, "No file was provided. Please select a file."

    ext = os.path.splitext(uploaded_file.name)[1].lower()
    if ext not in ALLOWED_FILE_EXTENSIONS:
        return False, f"Invalid file type '{ext}'. Allowed: {', '.join(ALLOWED_FILE_EXTENSIONS)}"

    if uploaded_file.size > MAX_FILE_SIZE:
        return False, "File size exceeds the 5 MB limit."

    return True, ""


def check_object_permission(request, obj):
    """Return True when the requesting user owns obj or is staff."""
    if hasattr(obj, 'created_by'):
        if obj.created_by != request.user and not request.user.is_staff:
            return False
    return True


# ─────────────────────────────────────────────
# Token tracking
# ─────────────────────────────────────────────
def log_token_usage(user, operation, prompt_tokens, completion_tokens,
                    total_tokens, model="gpt-4", cost=0.0):
    try:
        from .models import TokenUsageLog
        TokenUsageLog.objects.create(
            user=user, operation=operation,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            total_tokens=total_tokens, model=model, cost=cost,
        )
        logger.info(
            f"Token Usage — User: {user.username}, Op: {operation}, "
            f"Prompt: {prompt_tokens}, Completion: {completion_tokens}, "
            f"Total: {total_tokens}, Model: {model}, Cost: ${cost:.4f}"
        )
    except Exception as e:
        logger.error(f"Failed to log token usage: {e}")


def calculate_openai_cost(prompt_tokens, completion_tokens, model="gpt-4"):
    pricing = {
        'gpt-4':         {'prompt': 0.03,   'completion': 0.06},
        'gpt-4-turbo':   {'prompt': 0.01,   'completion': 0.03},
        'gpt-3.5-turbo': {'prompt': 0.0015, 'completion': 0.002},
    }
    p = pricing.get(model, pricing['gpt-4'])
    return (prompt_tokens / 1000) * p['prompt'] + (completion_tokens / 1000) * p['completion']


def get_user_token_stats(user, days=30):
    try:
        from .models import TokenUsageLog
        from datetime import timedelta
        since = timezone.now() - timedelta(days=days)
        logs = TokenUsageLog.objects.filter(user=user, created_at__gte=since)
        stats = {
            'total_tokens': sum(l.total_tokens for l in logs),
            'total_cost': sum(l.cost for l in logs),
            'operation_breakdown': {},
            'daily_usage': [],
        }
        for log in logs:
            op = stats['operation_breakdown'].setdefault(
                log.operation, {'count': 0, 'tokens': 0, 'cost': 0.0}
            )
            op['count'] += 1
            op['tokens'] += log.total_tokens
            op['cost']   += log.cost
        return stats
    except Exception as e:
        logger.error(f"Failed to get token stats: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
#  AUTH VIEWS — LOGIN / LOGOUT
# ═══════════════════════════════════════════════════════════════

@csrf_protect
@require_http_methods(["GET", "POST"])
def login_view(request):
    """
    Custom login view.
    • GET  → render the login form.
    • POST → authenticate; on success redirect to next or LOGIN_REDIRECT_URL.
    Already-authenticated users are forwarded immediately.
    """
    # Strip any query parameters (e.g. ?next=/) — clean URL only
    if request.GET:
        return redirect("login")

    # Redirect if already logged in
    if request.user.is_authenticated:
        return redirect(settings.LOGIN_REDIRECT_URL)

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")

        # Basic presence validation
        if not username or not password:
            messages.error(request, "Both username and password are required.")
            return render(request, "base/login.html", {"username": username})

        user = authenticate(request, username=username, password=password)

        if user is not None:
            if user.is_active:
                login(request, user)
                logger.info(f"User '{username}' logged in successfully.")
                return redirect(settings.LOGIN_REDIRECT_URL)
            else:
                messages.error(request, "Your account has been disabled. Contact an administrator.")
                logger.warning(f"Disabled account login attempt: '{username}'.")
        else:
            messages.error(request, "Invalid username or password.")
            logger.warning(f"Failed login attempt for username: '{username}'.")

        return render(request, "base/login.html", {"username": username})

    # GET
    return render(request, "base/login.html")


@login_required
@require_POST
@csrf_protect
def logout_view(request):
    """
    Custom logout view — POST only (protects against CSRF-based logouts).
    Clears the session and redirects to the login page.
    """
    username = request.user.username
    logout(request)
    logger.info(f"User '{username}' logged out.")
    messages.success(request, "You have been logged out successfully.")
    return redirect("login")


# ═══════════════════════════════════════════════════════════════
#  UPLOAD VIEW
# ═══════════════════════════════════════════════════════════════

@login_required
@csrf_protect
@require_http_methods(["GET", "POST"])
def upload_jd(request):
    """
    Upload and analyse a Job Description.
    Requires authentication. File is validated, text extracted, skills
    analysed via OpenAI, and results persisted to the DB.
    """
    if request.method == "POST":
        form = JDUploadForm(request.POST, request.FILES)

        if form.is_valid():
            uploaded_file = request.FILES.get("file")

            if not uploaded_file:
                messages.error(request, "Please select a file to upload.")
                logger.warning(f"No file provided by user {request.user.id}.")
                return redirect("upload_jd")

            is_valid, error_msg = validate_file_upload(uploaded_file)
            if not is_valid:
                messages.error(request, error_msg)
                logger.warning(
                    f"Invalid upload by user {request.user.id}: {error_msg}"
                )
                return redirect("upload_jd")

            # ── Persist the JD record ──────────────────────────────────
            jd = form.save(commit=False)
            jd.created_by = request.user
            jd.file = uploaded_file
            jd.save()

            if not jd.file or not jd.file.name:
                messages.error(request, "File was not saved correctly. Please try again.")
                logger.error(f"File save failed for user {request.user.id}.")
                jd.delete()
                return redirect("upload_jd")

            domain    = request.POST.get("domain", "")
            file_path = jd.file.path

            try:
                # ── Text extraction ────────────────────────────────────
                jd_text = extract_text_from_file(file_path)

                if not jd_text:
                    messages.error(request, "Could not extract text from the uploaded file.")
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    jd.delete()
                    logger.error(f"Text extraction failed for JD {jd.id}.")
                    return redirect("upload_jd")

                jd.jd_text = jd_text

                # ── AI skill extraction ────────────────────────────────
                result = extract_skills_from_jd(jd_text, domain)

                if "token_usage" in result:
                    tu    = result["token_usage"]
                    model = result.get("model", "gpt-4")
                    cost  = calculate_openai_cost(
                        tu["prompt_tokens"], tu["completion_tokens"], model
                    )
                    log_token_usage(
                        user=request.user,
                        operation="skill_extraction",
                        prompt_tokens=tu["prompt_tokens"],
                        completion_tokens=tu["completion_tokens"],
                        total_tokens=tu["total_tokens"],
                        model=model,
                        cost=cost,
                    )
                    messages.info(
                        request,
                        f"AI Analysis: {tu['total_tokens']} tokens used (${cost:.4f})"
                    )

                # ── Build LinkedIn search strings ──────────────────────
                linkedin_skills = result.get(
                    "linkedin_optimized_skills", result.get("all_skills", [])[:10]
                )
                search_strings = generate_linkedin_search_strings(
                    linkedin_skills,
                    jd.title,
                    result.get("experience_level", "Mid Level"),
                )

                # ── Persist extracted data ─────────────────────────────
                jd.all_skills            = ", ".join(result.get("all_skills", []))
                jd.linkedin_skills_string = ", ".join(linkedin_skills)
                jd.linkedin_search_string = json.dumps(search_strings)
                jd.skill_categories      = result.get("skill_categories", {})
                jd.role_category         = result.get("role_category", "Unknown")
                jd.experience_level      = result.get("experience_level", "Unknown")
                jd.key_responsibilities  = " | ".join(result.get("key_responsibilities", []))
                jd.qualifications        = (
                    " | ".join(result["qualifications"])
                    if isinstance(result.get("qualifications"), list)
                    else result.get("qualifications", "")
                )
                jd.save()

                # ── Excel export ───────────────────────────────────────
                save_jd_to_excel({
                    "Job Title":              jd.title,
                    "All Skills Required":    jd.all_skills,
                    "LinkedIn Search Skills": jd.linkedin_skills_string,
                    "LinkedIn Boolean Search": search_strings.get("basic_and", ""),
                    "Role Category":          jd.role_category,
                    "Experience Level":       jd.experience_level,
                    "Key Responsibilities":   jd.key_responsibilities,
                    "Qualifications":         jd.qualifications,
                    "Date Uploaded":          datetime.now().strftime("%Y-%m-%d"),
                    "Uploaded By":            request.user.username,
                })

                logger.info(f"JD {jd.id} analysed by user {request.user.id}.")
                messages.success(request, "Job Description analysed successfully!")
                return redirect("results", pk=jd.pk)

            except Exception as e:
                logger.error(f"Error processing JD by user {request.user.id}: {e}")
                messages.error(request, "An error occurred while processing the file. Please try again.")
                if os.path.exists(file_path):
                    os.remove(file_path)
                jd.delete()
                return redirect("upload_jd")

        # Form invalid — fall through to re-render with errors
    else:
        form = JDUploadForm()

    token_stats = get_user_token_stats(request.user, days=30)
    recent_jds  = (
        JobDescription.objects.all()[:10]
        if request.user.is_staff
        else JobDescription.objects.filter(created_by=request.user)[:10]
    )

    return render(request, "base/upload.html", {
        "form":        form,
        "recent_jds":  recent_jds,
        "token_stats": token_stats,
    })


# ═══════════════════════════════════════════════════════════════
#  REMAINING VIEWS (unchanged from original)
# ═══════════════════════════════════════════════════════════════



@login_required
@require_http_methods(["GET"])
def results(request, pk):
    jd = get_object_or_404(JobDescription, pk=pk)

    if not check_object_permission(request, jd):
        logger.warning(f"Unauthorised access to JD {pk} by user {request.user.id}.")
        raise PermissionDenied("You don't have permission to view this job description.")

    linkedin_searches = {}
    if jd.linkedin_search_string:
        try:
            linkedin_searches = json.loads(jd.linkedin_search_string)
        except json.JSONDecodeError:
            logger.error(f"Failed to parse LinkedIn search strings for JD {pk}.")

    api_available   = bool(getattr(settings, "CANDIDATES_API_BASE_URL", None))
    total_candidates = 0
    if api_available:
        try:
            df = fetch_candidates_from_api_initial()
            total_candidates = len(df) if not df.empty else 0
        except Exception as e:
            logger.warning(f"Failed to fetch candidate count: {e}")

    return render(request, "base/results.html", {
        "jd":               jd,
        "all_skills":       jd.get_all_skills_list(),
        "linkedin_skills":  jd.get_linkedin_skills_list(),
        "linkedin_searches": linkedin_searches,
        "skill_categories": jd.skill_categories,
        "responsibilities": jd.get_responsibilities_list(),
        "qualifications":   jd.get_qualifications_list(),
        "match_form":       CandidateMatchForm(),
        "api_available":    api_available,
        "total_candidates": total_candidates,
    })


@login_required
@require_POST
@csrf_protect
def match_candidates(request, jd_pk):
    jd = get_object_or_404(JobDescription, pk=jd_pk)

    if not check_object_permission(request, jd):
        raise PermissionDenied("You don't have permission to match candidates for this job description.")

    form = CandidateMatchForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Invalid form data. Please check your inputs.")
        return redirect("results", pk=jd.pk)

    min_match       = form.cleaned_data["min_match_percentage"]
    required_skills = jd.get_all_skills_list()

    if not required_skills:
        messages.error(request, "No skills found in the job description.")
        return redirect("results", pk=jd.pk)

    try:
        required_experience = None
        if hasattr(jd, "required_experience_years") and jd.required_experience_years:
            raw = jd.required_experience_years
            if isinstance(raw, str):
                try:
                    exp_data = json.loads(raw)
                    required_experience = exp_data.get("min", exp_data.get("max"))
                except Exception:
                    from .utils import parse_experience_years
                    required_experience = parse_experience_years(raw)
            elif isinstance(raw, dict):
                required_experience = raw.get("min")
            elif isinstance(raw, (int, float)):
                required_experience = int(raw)

        priority_skills    = jd.get_linkedin_skills_list() if hasattr(jd, "get_linkedin_skills_list") else required_skills[:10]
        api_filter_skills  = priority_skills or required_skills[:10]

        result = match_candidates_with_jd(
            required_skills=required_skills,
            min_match_percentage=min_match,
            jd_role_title=jd.title,
            required_experience=required_experience,
            api_filter_skills=api_filter_skills,
            max_candidates_from_api=500,
        )

        if isinstance(result, dict) and "candidates" in result:
            matched_candidates = result["candidates"]
            if "token_usage" in result:
                tu    = result["token_usage"]
                model = result.get("model", "gpt-4")
                cost  = calculate_openai_cost(tu["prompt_tokens"], tu["completion_tokens"], model)
                log_token_usage(
                    user=request.user, operation="candidate_matching",
                    prompt_tokens=tu["prompt_tokens"], completion_tokens=tu["completion_tokens"],
                    total_tokens=tu["total_tokens"], model=model, cost=cost,
                )
            if "api_stats" in result:
                s = result["api_stats"]
                messages.info(
                    request,
                    f"Fetched {s.get('total_fetched', 0)} candidates from API "
                    f"(filtered by {len(api_filter_skills)} skills, "
                    f"experience: {required_experience or 'any'})"
                )
        else:
            matched_candidates = result

        if not matched_candidates:
            messages.warning(request, "No candidates found. Try lowering the match percentage.")
            return redirect("results", pk=jd.pk)

        success, message = export_matched_candidates(request, matched_candidates, jd_pk)
        if not success:
            messages.error(request, message)
            return redirect("results", pk=jd.pk)

        session_candidates = [
            {
                "id":                   c.get("id", "N/A"),
                "name":                 c["name"],
                "email":                c["email"],
                "contact":              c["contact"],
                "designation":          c["designation"],
                "current_company":      c.get("current_company", "N/A"),
                "experience":           c["experience"],
                "location":             c["location"],
                "linkedin":             c["linkedin"],
                "qualification":        c.get("qualification", "N/A"),
                "match_percentage":     c["match_percentage"],
                "matched_skills_count": c["matched_skills_count"],
                "total_required_skills": c["total_required_skills"],
                "matched_skills":       c["matched_skills"][:15],
                "cv_link":              c.get("cv_link", "N/A"),
                "status":               c.get("status", "Active"),
            }
            for c in matched_candidates[:MAX_SESSION_CANDIDATES]
        ]

        request.session.update({
            "matched_candidates": session_candidates,
            "total_matches":      len(matched_candidates),
            "jd_id":              jd.pk,
            "match_settings": {
                "min_match_percentage": min_match,
                "total_skills":         len(required_skills),
                "api_filter_skills":    api_filter_skills,
                "required_experience":  required_experience,
            },
            "sheet_name": "Matched Candidates",
        })

        messages.success(request, f"Found {len(matched_candidates)} matching candidates!")
        return redirect("show_matches", jd_pk=jd.pk)

    except Exception as e:
        logger.error(f"Error matching candidates for JD {jd_pk}: {e}")
        messages.error(request, f"An error occurred while matching candidates: {e}")
        return redirect("results", pk=jd.pk)


@login_required
@require_http_methods(["GET"])
def show_matches(request, jd_pk):
    jd = get_object_or_404(JobDescription, pk=jd_pk)

    if not check_object_permission(request, jd):
        raise PermissionDenied("You don't have permission to view these matches.")

    if request.session.get("jd_id") != jd_pk:
        messages.error(request, "Invalid session data. Please run the match again.")
        return redirect("results", pk=jd_pk)

    matched_candidates = request.session.get("matched_candidates", [])
    total_matches      = request.session.get("total_matches", len(matched_candidates))
    match_settings     = request.session.get("match_settings", {})

    avg_match  = (
        sum(c["match_percentage"] for c in matched_candidates) / len(matched_candidates)
        if matched_candidates else 0
    )
    top_match  = (
        max(matched_candidates, key=lambda x: x["match_percentage"])
        if matched_candidates else None
    )

    return render(request, "base/show_matches.html", {
        "jd":                  jd,
        "matched_candidates":  matched_candidates,
        "output_file":         request.session.get("excel_filename", ""),
        "sheet_name":          request.session.get("sheet_name", "Matched Candidates"),
        "total_matches":       total_matches,
        "match_settings":      match_settings,
        "avg_match_percentage": round(avg_match, 1),
        "top_match":           top_match,
        "displayed_count":     len(matched_candidates),
        "has_more":            total_matches > len(matched_candidates),
    })


@login_required
@require_http_methods(["GET"])
def download_matched_file(request, jd_pk):
    from io import BytesIO
    jd = get_object_or_404(JobDescription, pk=jd_pk)

    if not check_object_permission(request, jd):
        raise PermissionDenied("You don't have permission to download this file.")

    if request.session.get("jd_id") != jd_pk:
        messages.error(request, "File not found or session expired. Please run the match again.")
        return redirect("show_matches", jd_pk=jd_pk)

    file_data_b64 = request.session.get("excel_file_data")
    filename      = request.session.get("excel_filename", "matched_candidates.xlsx")

    if not file_data_b64:
        messages.error(request, "File not found or session expired. Please run the match again.")
        return redirect("show_matches", jd_pk=jd_pk)

    try:
        file_data = base64.b64decode(file_data_b64)
        response  = FileResponse(
            BytesIO(file_data),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        logger.info(f"File downloaded by user {request.user.id} for JD {jd_pk}.")
        return response
    except base64.binascii.Error as e:
        logger.error(f"Base64 decode error for user {request.user.id}: {e}")
        messages.error(request, "File data is corrupted. Please regenerate the matches.")
        return redirect("show_matches", jd_pk=jd_pk)
    except Exception as e:
        logger.error(f"Download error for user {request.user.id}: {e}")
        messages.error(request, f"Error downloading file: {e}")
        return redirect("show_matches", jd_pk=jd_pk)


@login_required
@require_http_methods(["GET"])
def test_api_connection(request):
    try:
        df = fetch_candidates_from_api(skills=["python", "javascript"], experience=2, page=1, limit=10)
        if df.empty:
            messages.warning(request, "API connection successful but no candidates found.")
        else:
            messages.success(request, f"API connection successful! Found {len(df)} candidates.")
            messages.info(request, f"Available columns: {', '.join(df.columns.tolist()[:10])}")
    except Exception as e:
        logger.error(f"API connection test failed: {e}")
        messages.error(request, f"API connection failed: {e}")
    return redirect("upload_jd")


@login_required
@require_http_methods(["GET"])
def test_api_connection_init(request):
    try:
        df = fetch_candidates_from_api_initial()
        if df.empty:
            messages.warning(request, "API connection successful but no candidates found.")
        else:
            messages.success(request, f"API connection successful! Found {len(df)} candidates.")
            messages.info(request, f"Available columns: {', '.join(df.columns.tolist()[:10])}")
    except Exception as e:
        logger.error(f"API connection test failed: {e}")
        messages.error(request, f"API connection failed: {e}")
    return redirect("upload_jd")


# ── Token Usage Dashboard ────────────────────────────────────────

def get_organization_stats():
    try:
        from .models import TokenUsageLog
        from datetime import timedelta
        since = timezone.now() - timedelta(days=30)
        stats = TokenUsageLog.objects.filter(created_at__gte=since).aggregate(
            total_tokens=Sum("total_tokens"),
            total_cost=Sum("cost"),
            total_users=Count("user", distinct=True),
            total_operations=Count("id"),
        )
        return {k: v or 0 for k, v in stats.items()}
    except Exception as e:
        logger.error(f"Failed to get org stats: {e}")
        return None


def calculate_usage_insights(stats_30, stats_7, stats_today):
    insights = {"trend": "stable", "efficiency_score": 0, "recommendations": []}
    try:
        if stats_30["total_tokens"] and stats_7["total_tokens"]:
            weekly_rate  = stats_7["total_tokens"] / 7
            monthly_rate = stats_30["total_tokens"] / 30
            if weekly_rate > monthly_rate * 1.5:
                insights["trend"] = "increasing"
                insights["recommendations"].append(
                    "Usage is higher than average this week. Consider optimising prompts."
                )
            elif weekly_rate < monthly_rate * 0.5:
                insights["trend"] = "decreasing"

        if stats_30["total_tokens"] and stats_30["total_cost"]:
            cost_per_1k = (stats_30["total_cost"] / stats_30["total_tokens"]) * 1000
            if cost_per_1k <= 0.0004:
                insights["efficiency_score"] = 100
            elif cost_per_1k <= 0.0006:
                insights["efficiency_score"] = 80
            elif cost_per_1k <= 0.001:
                insights["efficiency_score"] = 60
            else:
                insights["efficiency_score"] = 40
                insights["recommendations"].append(
                    "Consider using more efficient models or optimising prompt lengths."
                )

        if stats_30.get("operation_breakdown"):
            bd = stats_30["operation_breakdown"]
            insights["most_expensive_operation"] = max(bd, key=lambda k: bd[k]["cost"])
            if bd.get("skill_extraction", {}).get("count", 0) > 100:
                insights["recommendations"].append(
                    f"You performed {bd['skill_extraction']['count']} skill extractions. "
                    "Consider batching or caching results."
                )

        daily_avg = stats_30["total_tokens"] / 30 if stats_30["total_tokens"] else 0
        if stats_today["total_tokens"] and stats_today["total_tokens"] > daily_avg * 2:
            insights["recommendations"].append("Today's usage is unusually high. Monitor your operations.")
    except Exception as e:
        logger.error(f"Error calculating insights: {e}")
    return insights


@login_required
@require_http_methods(["GET"])
def token_usage_dashboard(request):
    user      = request.user
    cache_key = f"token_stats_{user.id}"
    cached    = cache.get(cache_key)

    if cached and not request.GET.get("refresh"):
        context = {**cached, "cached": True, "cache_time": cache.ttl(cache_key)}
    else:
        s30    = get_user_token_stats(user, days=30)
        s7     = get_user_token_stats(user, days=7)
        today  = get_user_token_stats(user, days=1)
        try:
            from .models import TokenUsageLog
            recent_logs = (
                TokenUsageLog.objects.filter(user=user)
                .select_related("user")
                .order_by("-created_at")[:50]
            )
        except Exception:
            recent_logs = []

        context = {
            "stats_30_days": s30,
            "stats_7_days":  s7,
            "stats_today":   today,
            "recent_logs":   recent_logs,
            "org_stats":     get_organization_stats() if user.is_staff else None,
            "insights":      calculate_usage_insights(s30, s7, today),
            "cached":        False,
        }
        cache.set(cache_key, context, 300)

    return render(request, "base/token_usage_dashboard.html", context)


@login_required
@require_POST
def clear_token_cache(request):
    cache.delete(f"token_stats_{request.user.id}")
    messages.success(request, "Token statistics refreshed successfully!")
    return redirect("token_usage_dashboard")


def google_verify(request):
    file_path = os.path.join(settings.BASE_DIR, "static/google63d0dde2db21043b.html")
    return FileResponse(open(file_path, "rb"), content_type="text/html")