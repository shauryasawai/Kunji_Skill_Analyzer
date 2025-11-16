from django.db import models
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
from pathlib import Path
import json
import os

class JobDescription(models.Model):
    """Model to store job descriptions and extracted skills"""
    
    title = models.CharField(max_length=200, help_text="Job title/position")
    file = models.FileField(
        upload_to='jd_uploads/%Y/%m/',
        help_text="Upload JD file (PDF, DOCX, TXT)",
        blank=True,
        null=True
    )
    jd_text = models.TextField(
        blank=True,
        help_text="Extracted text from JD file"
    )
    
    # Skills fields
    all_skills = models.TextField(
        blank=True,
        help_text="Comma-separated list of all extracted skills"
    )
    linkedin_skills_string = models.TextField(
        blank=True,
        help_text="LinkedIn-optimized skills string"
    )
    linkedin_search_string = models.TextField(
        blank=True,
        help_text="JSON object containing various LinkedIn search strings"
    )
    skill_categories = models.JSONField(
        default=dict,
        blank=True,
        help_text="Skills organized by category"
    )
    
    # Additional metadata
    role_category = models.CharField(
        max_length=100,
        blank=True,
        help_text="Category of role (e.g., IT, HR, Marketing)"
    )
    experience_level = models.CharField(
        max_length=50,
        blank=True,
        help_text="Required experience level"
    )
    key_responsibilities = models.TextField(
        blank=True,
        help_text="Key responsibilities from JD"
    )
    qualifications = models.TextField(
        blank=True,
        help_text="Required qualifications"
    )
    
    # Audit fields
    created_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='job_descriptions',
        help_text="User who uploaded this JD"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Matching statistics
    total_matches_run = models.IntegerField(
        default=0,
        help_text="Number of times matching was run for this JD"
    )
    last_matched_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last time candidates were matched"
    )
    best_match_percentage = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Best match percentage found"
    )
    total_candidates_matched = models.IntegerField(
        default=0,
        help_text="Total candidates matched across all runs"
    )
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = "Job Description"
        verbose_name_plural = "Job Descriptions"
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['created_by', '-created_at']),
            models.Index(fields=['role_category']),
        ]
    
    def __str__(self):
        return f"{self.title} - {self.created_at.strftime('%Y-%m-%d')}"
    
    def save(self, *args, **kwargs):
        """Override save to delete file after processing"""
        super().save(*args, **kwargs)
        
        # Delete uploaded file after skills are extracted (security measure)
        if self.file and self.all_skills and os.path.exists(self.file.path):
            try:
                os.remove(self.file.path)
                self.file = None
                # Save again without triggering recursion
                super().save(update_fields=['file'])
            except Exception as e:
                print(f"Error deleting file: {e}")
    
    def get_all_skills_list(self):
        """Return all skills as a list"""
        if self.all_skills:
            return [s.strip() for s in self.all_skills.split(',') if s.strip()]
        return []
    
    def get_linkedin_skills_list(self):
        """Return LinkedIn skills as a list"""
        if self.linkedin_skills_string:
            return [s.strip() for s in self.linkedin_skills_string.split(',') if s.strip()]
        return []
    
    def get_responsibilities_list(self):
        """Return responsibilities as a list"""
        if self.key_responsibilities:
            return [r.strip() for r in self.key_responsibilities.split('|') if r.strip()]
        return []
    
    def get_qualifications_list(self):
        """Return qualifications as a list"""
        if self.qualifications:
            return [q.strip() for q in self.qualifications.split('|') if q.strip()]
        return []
    
    def get_linkedin_searches_dict(self):
        """Return LinkedIn search strings as dict"""
        if self.linkedin_search_string:
            try:
                return json.loads(self.linkedin_search_string)
            except json.JSONDecodeError:
                return {}
        return {}
    
    def update_match_statistics(self, num_matched, best_percentage):
        """Update matching statistics"""
        self.total_matches_run += 1
        self.last_matched_at = timezone.now()
        self.total_candidates_matched += num_matched
        
        if self.best_match_percentage is None or best_percentage > self.best_match_percentage:
            self.best_match_percentage = best_percentage
        
        self.save(update_fields=[
            'total_matches_run',
            'last_matched_at',
            'total_candidates_matched',
            'best_match_percentage'
        ])
    
    def get_skill_count(self):
        """Return total number of skills"""
        return len(self.get_all_skills_list())
    
    def get_linkedin_skill_count(self):
        """Return number of LinkedIn-optimized skills"""
        return len(self.get_linkedin_skills_list())


class MatchingHistory(models.Model):
    """Track history of candidate matching runs"""
    
    job_description = models.ForeignKey(
        JobDescription,
        on_delete=models.CASCADE,
        related_name='matching_history'
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='matching_runs'
    )
    
    # Matching parameters
    min_match_percentage = models.FloatField(
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Minimum match percentage used"
    )
    use_fuzzy_matching = models.BooleanField(
        default=True,
        help_text="Whether fuzzy matching was enabled"
    )
    fuzzy_threshold = models.IntegerField(
        default=85,
        validators=[MinValueValidator(50), MaxValueValidator(100)],
        help_text="Fuzzy matching threshold used"
    )
    
    # Results
    total_candidates_found = models.IntegerField(
        default=0,
        help_text="Total candidates found matching criteria"
    )
    average_match_percentage = models.FloatField(
        null=True,
        blank=True,
        help_text="Average match percentage of found candidates"
    )
    best_match_percentage = models.FloatField(
        null=True,
        blank=True,
        help_text="Best match percentage found"
    )
    best_match_candidate_name = models.CharField(
        max_length=200,
        blank=True,
        help_text="Name of best matching candidate"
    )
    
    # API metadata
    api_response_time_ms = models.IntegerField(
        null=True,
        blank=True,
        help_text="API response time in milliseconds"
    )
    total_api_candidates = models.IntegerField(
        null=True,
        blank=True,
        help_text="Total candidates available in API"
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = "Matching History"
        verbose_name_plural = "Matching Histories"
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['job_description', '-created_at']),
            models.Index(fields=['user', '-created_at']),
        ]
    
    def __str__(self):
        return f"{self.job_description.title} - {self.total_candidates_found} matches - {self.created_at.strftime('%Y-%m-%d %H:%M')}"


class APIConfiguration(models.Model):
    """Store API configuration and settings"""
    
    name = models.CharField(
        max_length=100,
        unique=True,
        help_text="Configuration name (e.g., 'Production API', 'Staging API')"
    )
    api_url = models.URLField(
        max_length=500,
        help_text="API endpoint URL"
    )
    api_key = models.CharField(
        max_length=500,
        blank=True,
        help_text="API authentication key (optional)"
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this configuration is currently active"
    )
    
    # Connection test
    last_tested_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last time API connection was tested"
    )
    last_test_status = models.CharField(
        max_length=20,
        choices=[
            ('success', 'Success'),
            ('failed', 'Failed'),
            ('pending', 'Pending')
        ],
        default='pending',
        help_text="Status of last connection test"
    )
    last_test_message = models.TextField(
        blank=True,
        help_text="Message from last connection test"
    )
    total_candidates_available = models.IntegerField(
        null=True,
        blank=True,
        help_text="Total candidates available in API"
    )
    
    # Usage statistics
    total_requests = models.IntegerField(
        default=0,
        help_text="Total API requests made"
    )
    total_successful_requests = models.IntegerField(
        default=0,
        help_text="Total successful API requests"
    )
    total_failed_requests = models.IntegerField(
        default=0,
        help_text="Total failed API requests"
    )
    average_response_time_ms = models.FloatField(
        null=True,
        blank=True,
        help_text="Average API response time in milliseconds"
    )
    
    # Audit fields
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='api_configurations'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-is_active', 'name']
        verbose_name = "API Configuration"
        verbose_name_plural = "API Configurations"
    
    def __str__(self):
        status = "✓ Active" if self.is_active else "✗ Inactive"
        return f"{self.name} ({status})"
    
    def test_connection(self):
        """Test API connection and update status"""
        from .utils import fetch_candidates_from_api
        import time
        
        try:
            start_time = time.time()
            df = fetch_candidates_from_api(api_url=self.api_url, api_key=self.api_key)
            response_time = int((time.time() - start_time) * 1000)
            
            if not df.empty:
                self.last_test_status = 'success'
                self.last_test_message = f"Successfully connected. Found {len(df)} candidates."
                self.total_candidates_available = len(df)
                self.total_successful_requests += 1
                
                # Update average response time
                if self.average_response_time_ms:
                    self.average_response_time_ms = (self.average_response_time_ms + response_time) / 2
                else:
                    self.average_response_time_ms = response_time
            else:
                self.last_test_status = 'success'
                self.last_test_message = "Connected successfully but no candidates found."
                self.total_candidates_available = 0
                self.total_successful_requests += 1
            
            self.last_tested_at = timezone.now()
            self.total_requests += 1
            self.save()
            
            return True, self.last_test_message
            
        except Exception as e:
            self.last_test_status = 'failed'
            self.last_test_message = f"Connection failed: {str(e)}"
            self.last_tested_at = timezone.now()
            self.total_requests += 1
            self.total_failed_requests += 1
            self.save()
            
            return False, self.last_test_message
    
    def record_request(self, success=True, response_time_ms=None):
        """Record an API request"""
        self.total_requests += 1
        
        if success:
            self.total_successful_requests += 1
        else:
            self.total_failed_requests += 1
        
        if response_time_ms and self.average_response_time_ms:
            self.average_response_time_ms = (self.average_response_time_ms + response_time_ms) / 2
        elif response_time_ms:
            self.average_response_time_ms = response_time_ms
        
        self.save(update_fields=[
            'total_requests',
            'total_successful_requests',
            'total_failed_requests',
            'average_response_time_ms'
        ])
    
    def get_success_rate(self):
        """Calculate API success rate percentage"""
        if self.total_requests == 0:
            return 0
        return (self.total_successful_requests / self.total_requests) * 100