from django.utils.deprecation import MiddlewareMixin
from django.shortcuts import redirect
from django.contrib import messages
from .models import AuditLog
import logging

logger = logging.getLogger(__name__)


class AuditLogMiddleware(MiddlewareMixin):
    """Log important user actions for security auditing"""
    
    def process_request(self, request):
        # Store IP address and user agent for logging
        request.ip_address = self.get_client_ip(request)
        request.user_agent = request.META.get('HTTP_USER_AGENT', '')
    
    def process_response(self, request, response):
        # Log successful actions
        if hasattr(request, 'user') and request.user.is_authenticated:
            if hasattr(request, 'audit_action'):
                self.log_action(request, request.audit_action)
        
        return response
    
    def process_exception(self, request, exception):
        # Log permission denied attempts
        if exception.__class__.__name__ == 'PermissionDenied':
            if hasattr(request, 'user') and request.user.is_authenticated:
                self.log_action(request, 'PERMISSION_DENIED', {
                    'exception': str(exception),
                    'path': request.path,
                })
        
        return None
    
    @staticmethod
    def get_client_ip(request):
        """Get real client IP address"""
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0]
        else:
            ip = request.META.get('REMOTE_ADDR')
        return ip
    
    @staticmethod
    def log_action(request, action, details=None):
        """Create audit log entry"""
        try:
            AuditLog.objects.create(
                user=request.user if request.user.is_authenticated else None,
                action=action,
                ip_address=request.ip_address,
                user_agent=request.user_agent,
                details=details or {}
            )
        except Exception as e:
            logger.error(f"Failed to create audit log: {str(e)}")


class SecurityHeadersMiddleware(MiddlewareMixin):
    """Add additional security headers"""
    
    def process_response(self, request, response):
        # Content Security Policy
        response['Content-Security-Policy'] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https:; "
            "connect-src 'self'; "
        )
        
        # Referrer Policy
        response['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        
        # Permissions Policy
        response['Permissions-Policy'] = (
            "geolocation=(), "
            "microphone=(), "
            "camera=(), "
            "payment=(), "
            "usb=(), "
            "magnetometer=(), "
            "gyroscope=(), "
            "accelerometer=()"
        )
        
        return response


class RateLimitMiddleware(MiddlewareMixin):
    """Simple rate limiting for sensitive operations"""
    
    def __init__(self, get_response):
        self.get_response = get_response
        self.rate_limit_cache = {}
    
    def __call__(self, request):
        # Check rate limit for POST requests
        if request.method == 'POST' and request.user.is_authenticated:
            user_id = request.user.id
            current_time = int(time.time())
            
            # Create user key
            user_key = f"user_{user_id}"
            
            if user_key not in self.rate_limit_cache:
                self.rate_limit_cache[user_key] = []
            
            # Clean old entries (older than 1 minute)
            self.rate_limit_cache[user_key] = [
                timestamp for timestamp in self.rate_limit_cache[user_key]
                if current_time - timestamp < 60
            ]
            
            # Check if rate limit exceeded (max 30 requests per minute)
            if len(self.rate_limit_cache[user_key]) >= 30:
                logger.warning(f"Rate limit exceeded for user {user_id}")
                messages.error(request, "Too many requests. Please slow down.")
                return redirect(request.META.get('HTTP_REFERER', '/'))
            
            # Add current request
            self.rate_limit_cache[user_key].append(current_time)
        
        response = self.get_response(request)
        return response


import time


class SessionSecurityMiddleware(MiddlewareMixin):
    """Enhanced session security"""
    
    def process_request(self, request):
        if request.user.is_authenticated:
            # Check if IP address changed (potential session hijacking)
            session_ip = request.session.get('user_ip')
            current_ip = self.get_client_ip(request)
            
            if session_ip and session_ip != current_ip:
                logger.warning(
                    f"IP address mismatch for user {request.user.id}: "
                    f"session_ip={session_ip}, current_ip={current_ip}"
                )
                # Optionally log out user
                # auth_logout(request)
                # messages.warning(request, "Your session has been terminated for security reasons.")
                # return redirect('login')
            
            # Store current IP
            request.session['user_ip'] = current_ip
            
            # Update last activity
            request.session['last_activity'] = int(time.time())
    
    @staticmethod
    def get_client_ip(request):
        """Get real client IP address"""
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0]
        else:
            ip = request.META.get('REMOTE_ADDR')
        return ip

class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response['Content-Security-Policy'] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "frame-ancestors 'none';"
        )
        response['X-Content-Type-Options'] = 'nosniff'
        response['X-Frame-Options'] = 'DENY'
        response['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        return response