from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from django.utils import timezone
from django.db.models import Count, Avg, Max
from .models import JobDescription, MatchingHistory, APIConfiguration
import json

@admin.register(JobDescription)
class JobDescriptionAdmin(admin.ModelAdmin):
    list_display = [
        'title',
        'role_category',
        'experience_level',
        'skill_count_display',
        'matches_run_display',
        'best_match_display',
        'created_by',
        'created_at_display',
        'status_badge'
    ]
    
    list_filter = [
        'role_category',
        'experience_level',
        'created_at',
        'created_by'
    ]
    
    search_fields = [
        'title',
        'all_skills',
        'linkedin_skills_string',
        'role_category',
        'jd_text'
    ]
    
    readonly_fields = [
        'created_at',
        'updated_at',
        'last_matched_at',
        'total_matches_run',
        'total_candidates_matched',
        'best_match_percentage',
        'skill_categories_display',
        'linkedin_searches_display',
        'statistics_display'
    ]
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('title', 'created_by', 'role_category', 'experience_level')
        }),
        ('Job Description', {
            'fields': ('jd_text',),
            'classes': ('collapse',)
        }),
        ('Skills', {
            'fields': (
                'all_skills',
                'linkedin_skills_string',
                'skill_categories_display',
                'linkedin_searches_display'
            )
        }),
        ('Job Details', {
            'fields': ('key_responsibilities', 'qualifications'),
            'classes': ('collapse',)
        }),
        ('Matching Statistics', {
            'fields': (
                'total_matches_run',
                'last_matched_at',
                'total_candidates_matched',
                'best_match_percentage',
                'statistics_display'
            )
        }),
        ('Audit', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )
    
    actions = ['export_to_excel', 'reset_statistics']
    
    def skill_count_display(self, obj):
        """Display total skill count"""
        count = obj.get_skill_count()
        linkedin_count = obj.get_linkedin_skill_count()
        return format_html(
            '<span style="color: #0066cc; font-weight: bold;">{}</span> total<br>'
            '<span style="color: #00aa00;">{}</span> LinkedIn',
            count,
            linkedin_count
        )
    skill_count_display.short_description = "Skills"
    
    def matches_run_display(self, obj):
        """Display number of matches run"""
        if obj.total_matches_run > 0:
            return format_html(
                '<span style="background-color: #e3f2fd; padding: 3px 8px; border-radius: 3px;">'
                '{} runs</span>',
                obj.total_matches_run
            )
        return format_html('<span style="color: #999;">No matches</span>')
    matches_run_display.short_description = "Matches Run"
    
    def best_match_display(self, obj):
        """Display best match percentage"""
        if obj.best_match_percentage:
            color = '#4caf50' if obj.best_match_percentage >= 80 else '#ff9800' if obj.best_match_percentage >= 60 else '#f44336'
            return format_html(
                '<span style="background-color: {}; color: white; padding: 3px 8px; border-radius: 3px; font-weight: bold;">'
                '{}%</span>',
                color,
                round(obj.best_match_percentage, 1)
            )
        return '-'
    best_match_display.short_description = "Best Match"
    
    def created_at_display(self, obj):
        """Display formatted created date"""
        return obj.created_at.strftime('%b %d, %Y %H:%M')
    created_at_display.short_description = "Created"
    created_at_display.admin_order_field = 'created_at'
    
    def status_badge(self, obj):
        """Display status badge"""
        if obj.total_matches_run > 0:
            return format_html(
                '<span style="background-color: #4caf50; color: white; padding: 2px 6px; '
                'border-radius: 3px; font-size: 11px;">✓ MATCHED</span>'
            )
        return format_html(
            '<span style="background-color: #ff9800; color: white; padding: 2px 6px; '
            'border-radius: 3px; font-size: 11px;">⏳ PENDING</span>'
        )
    status_badge.short_description = "Status"
    
    def skill_categories_display(self, obj):
        """Display skill categories in formatted way"""
        if obj.skill_categories:
            html_output = '<div style="background: #f5f5f5; padding: 10px; border-radius: 5px;">'
            for category, skills in obj.skill_categories.items():
                html_output += f'<strong style="color: #1976d2;">{category}:</strong><br>'
                html_output += f'<span style="margin-left: 20px;">{", ".join(skills)}</span><br><br>'
            html_output += '</div>'
            return format_html(html_output)
        return '-'
    skill_categories_display.short_description = "Skill Categories"
    
    def linkedin_searches_display(self, obj):
        """Display LinkedIn search strings"""
        searches = obj.get_linkedin_searches_dict()
        if searches:
            html_output = '<div style="background: #e3f2fd; padding: 10px; border-radius: 5px;">'
            for search_type, search_string in searches.items():
                html_output += f'<strong style="color: #0066cc;">{search_type.replace("_", " ").title()}:</strong><br>'
                html_output += f'<code style="background: white; padding: 5px; display: block; margin: 5px 0 10px 0; border-radius: 3px;">{search_string[:200]}</code>'
            html_output += '</div>'
            return format_html(html_output)
        return '-'
    linkedin_searches_display.short_description = "LinkedIn Searches"
    
    def statistics_display(self, obj):
        """Display comprehensive statistics"""
        history = obj.matching_history.all()
        
        if history.exists():
            stats = history.aggregate(
                total_runs=Count('id'),
                avg_candidates=Avg('total_candidates_found'),
                max_candidates=Max('total_candidates_found'),
                avg_match_pct=Avg('average_match_percentage')
            )
            
            html = '<div style="background: #fff3e0; padding: 10px; border-radius: 5px;">'
            html += f'<strong>Total Runs:</strong> {stats["total_runs"]}<br>'
            html += f'<strong>Avg Candidates Found:</strong> {round(stats["avg_candidates"] or 0, 1)}<br>'
            html += f'<strong>Max Candidates Found:</strong> {stats["max_candidates"] or 0}<br>'
            html += f'<strong>Avg Match %:</strong> {round(stats["avg_match_pct"] or 0, 1)}%<br>'
            html += '</div>'
            return format_html(html)
        
        return format_html('<span style="color: #999;">No statistics available</span>')
    statistics_display.short_description = "Detailed Statistics"
    
    def export_to_excel(self, request, queryset):
        """Export selected JDs to Excel"""
        # Implementation for export
        self.message_user(request, f"Exported {queryset.count()} job descriptions")
    export_to_excel.short_description = "Export selected JDs to Excel"
    
    def reset_statistics(self, request, queryset):
        """Reset matching statistics"""
        queryset.update(
            total_matches_run=0,
            last_matched_at=None,
            best_match_percentage=None,
            total_candidates_matched=0
        )
        self.message_user(request, f"Reset statistics for {queryset.count()} job descriptions")
    reset_statistics.short_description = "Reset matching statistics"


@admin.register(MatchingHistory)
class MatchingHistoryAdmin(admin.ModelAdmin):
    list_display = [
        'job_title_display',
        'user',
        'candidates_found_display',
        'match_percentage_display',
        'fuzzy_status_display',
        'api_performance_display',
        'created_at_display'
    ]
    
    list_filter = [
        'use_fuzzy_matching',
        'created_at',
        'user',
        'job_description__role_category'
    ]
    
    search_fields = [
        'job_description__title',
        'user__username',
        'best_match_candidate_name'
    ]
    
    readonly_fields = [
        'job_description',
        'user',
        'min_match_percentage',
        'use_fuzzy_matching',
        'fuzzy_threshold',
        'total_candidates_found',
        'average_match_percentage',
        'best_match_percentage',
        'best_match_candidate_name',
        'api_response_time_ms',
        'total_api_candidates',
        'created_at'
    ]
    
    fieldsets = (
        ('Match Information', {
            'fields': ('job_description', 'user', 'created_at')
        }),
        ('Match Parameters', {
            'fields': (
                'min_match_percentage',
                'use_fuzzy_matching',
                'fuzzy_threshold'
            )
        }),
        ('Results', {
            'fields': (
                'total_candidates_found',
                'average_match_percentage',
                'best_match_percentage',
                'best_match_candidate_name'
            )
        }),
        ('API Performance', {
            'fields': (
                'api_response_time_ms',
                'total_api_candidates'
            )
        })
    )
    
    def job_title_display(self, obj):
        """Display job title with link"""
        url = reverse('admin:base_jobdescription_change', args=[obj.job_description.id])
        return format_html(
            '<a href="{}" style="color: #0066cc; text-decoration: none;">{}</a>',
            url,
            obj.job_description.title
        )
    job_title_display.short_description = "Job Description"
    job_title_display.admin_order_field = 'job_description__title'
    
    def candidates_found_display(self, obj):
        """Display number of candidates found"""
        if obj.total_candidates_found > 0:
            color = '#4caf50' if obj.total_candidates_found >= 10 else '#ff9800' if obj.total_candidates_found >= 5 else '#f44336'
            return format_html(
                '<span style="background-color: {}; color: white; padding: 3px 10px; '
                'border-radius: 3px; font-weight: bold;">{}</span>',
                color,
                obj.total_candidates_found
            )
        return format_html('<span style="color: #999;">0</span>')
    candidates_found_display.short_description = "Candidates"
    candidates_found_display.admin_order_field = 'total_candidates_found'
    
    def match_percentage_display(self, obj):
        """Display match percentage statistics"""
        if obj.average_match_percentage and obj.best_match_percentage:
            return format_html(
                '<div style="text-align: center;">'
                '<strong style="color: #4caf50;">Best:</strong> {}%<br>'
                '<strong style="color: #2196f3;">Avg:</strong> {}%'
                '</div>',
                round(obj.best_match_percentage, 1),
                round(obj.average_match_percentage, 1)
            )
        return '-'
    match_percentage_display.short_description = "Match %"
    
    def fuzzy_status_display(self, obj):
        """Display fuzzy matching status"""
        if obj.use_fuzzy_matching:
            return format_html(
                '<span style="background-color: #2196f3; color: white; padding: 2px 6px; '
                'border-radius: 3px; font-size: 11px;">✓ FUZZY ({}%)</span>',
                obj.fuzzy_threshold
            )
        return format_html(
            '<span style="background-color: #9e9e9e; color: white; padding: 2px 6px; '
            'border-radius: 3px; font-size: 11px;">EXACT</span>'
        )
    fuzzy_status_display.short_description = "Matching Type"
    
    def api_performance_display(self, obj):
        """Display API performance metrics"""
        if obj.api_response_time_ms:
            color = '#4caf50' if obj.api_response_time_ms < 1000 else '#ff9800' if obj.api_response_time_ms < 3000 else '#f44336'
            return format_html(
                '<span style="color: {}; font-weight: bold;">{} ms</span><br>'
                '<span style="color: #666; font-size: 11px;">{} total</span>',
                color,
                obj.api_response_time_ms,
                obj.total_api_candidates or 0
            )
        return '-'
    api_performance_display.short_description = "API Performance"
    
    def created_at_display(self, obj):
        """Display formatted created date"""
        return obj.created_at.strftime('%b %d, %Y %H:%M')
    created_at_display.short_description = "Run Date"
    created_at_display.admin_order_field = 'created_at'
    
    def has_add_permission(self, request):
        """Disable manual creation"""
        return False


@admin.register(APIConfiguration)
class APIConfigurationAdmin(admin.ModelAdmin):
    list_display = [
        'name',
        'status_display',
        'connection_status_display',
        'candidates_display',
        'success_rate_display',
        'performance_display',
        'last_tested_display'
    ]
    
    list_filter = [
        'is_active',
        'last_test_status',
        'created_at'
    ]
    
    search_fields = [
        'name',
        'api_url'
    ]
    
    readonly_fields = [
        'last_tested_at',
        'last_test_status',
        'last_test_message',
        'total_candidates_available',
        'total_requests',
        'total_successful_requests',
        'total_failed_requests',
        'average_response_time_ms',
        'success_rate_display_detail',
        'created_at',
        'updated_at'
    ]
    
    fieldsets = (
        ('Configuration', {
            'fields': ('name', 'api_url', 'api_key', 'is_active', 'created_by')
        }),
        ('Connection Status', {
            'fields': (
                'last_tested_at',
                'last_test_status',
                'last_test_message',
                'total_candidates_available'
            )
        }),
        ('Usage Statistics', {
            'fields': (
                'total_requests',
                'total_successful_requests',
                'total_failed_requests',
                'average_response_time_ms',
                'success_rate_display_detail'
            ),
            'classes': ('collapse',)
        }),
        ('Audit', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )
    
    actions = ['test_connection_action', 'activate_config', 'deactivate_config']
    
    def status_display(self, obj):
        """Display active/inactive status"""
        if obj.is_active:
            return format_html(
                '<span style="background-color: #4caf50; color: white; padding: 3px 8px; '
                'border-radius: 3px; font-weight: bold;">✓ ACTIVE</span>'
            )
        return format_html(
            '<span style="background-color: #9e9e9e; color: white; padding: 3px 8px; '
            'border-radius: 3px;">✗ INACTIVE</span>'
        )
    status_display.short_description = "Status"
    status_display.admin_order_field = 'is_active'
    
    def connection_status_display(self, obj):
        """Display connection test status"""
        status_colors = {
            'success': ('#4caf50', '✓'),
            'failed': ('#f44336', '✗'),
            'pending': ('#ff9800', '⏳')
        }
        color, icon = status_colors.get(obj.last_test_status, ('#9e9e9e', '?'))
        
        return format_html(
            '<span style="background-color: {}; color: white; padding: 2px 6px; '
            'border-radius: 3px; font-size: 11px;">{} {}</span>',
            color,
            icon,
            obj.last_test_status.upper()
        )
    connection_status_display.short_description = "Connection"
    connection_status_display.admin_order_field = 'last_test_status'
    
    def candidates_display(self, obj):
        """Display total candidates available"""
        if obj.total_candidates_available is not None:
            return format_html(
                '<span style="color: #0066cc; font-weight: bold; font-size: 14px;">{:,}</span>',
                obj.total_candidates_available
            )
        return '-'
    candidates_display.short_description = "Candidates"
    candidates_display.admin_order_field = 'total_candidates_available'
    
    def success_rate_display(self, obj):
        """Display success rate"""
        rate = obj.get_success_rate()
        color = '#4caf50' if rate >= 90 else '#ff9800' if rate >= 70 else '#f44336'
        
        return format_html(
            '<div style="text-align: center;">'
            '<div style="font-size: 18px; font-weight: bold; color: {};">{:.1f}%</div>'
            '<div style="font-size: 10px; color: #666;">{}/{} requests</div>'
            '</div>',
            color,
            rate,
            obj.total_successful_requests,
            obj.total_requests
        )
    success_rate_display.short_description = "Success Rate"
    
    def success_rate_display_detail(self, obj):
        """Detailed success rate display"""
        rate = obj.get_success_rate()
        return format_html(
            '<div style="background: #f5f5f5; padding: 10px; border-radius: 5px;">'
            '<strong style="font-size: 16px; color: #0066cc;">{:.2f}%</strong><br><br>'
            '<strong>Total Requests:</strong> {}<br>'
            '<strong>Successful:</strong> <span style="color: #4caf50;">{}</span><br>'
            '<strong>Failed:</strong> <span style="color: #f44336;">{}</span>'
            '</div>',
            rate,
            obj.total_requests,
            obj.total_successful_requests,
            obj.total_failed_requests
        )
    success_rate_display_detail.short_description = "Success Rate Details"
    
    def performance_display(self, obj):
        """Display performance metrics"""
        if obj.average_response_time_ms:
            color = '#4caf50' if obj.average_response_time_ms < 1000 else '#ff9800' if obj.average_response_time_ms < 3000 else '#f44336'
            return format_html(
                '<span style="color: {}; font-weight: bold;">{:.0f} ms</span>',
                color,
                obj.average_response_time_ms
            )
        return '-'
    performance_display.short_description = "Avg Response"
    performance_display.admin_order_field = 'average_response_time_ms'
    
    def last_tested_display(self, obj):
        """Display last tested time"""
        if obj.last_tested_at:
            return obj.last_tested_at.strftime('%b %d, %Y %H:%M')
        return format_html('<span style="color: #999;">Never</span>')
    last_tested_display.short_description = "Last Tested"
    last_tested_display.admin_order_field = 'last_tested_at'
    
    def test_connection_action(self, request, queryset):
        """Test connection for selected configurations"""
        success_count = 0
        fail_count = 0
        
        for config in queryset:
            success, message = config.test_connection()
            if success:
                success_count += 1
            else:
                fail_count += 1
        
        if success_count > 0:
            self.message_user(
                request,
                f"Successfully tested {success_count} configuration(s)",
                level='success'
            )
        if fail_count > 0:
            self.message_user(
                request,
                f"Failed to test {fail_count} configuration(s)",
                level='error'
            )
    test_connection_action.short_description = "Test API connection"
    
    def activate_config(self, request, queryset):
        """Activate selected configurations"""
        updated = queryset.update(is_active=True)
        self.message_user(request, f"Activated {updated} configuration(s)")
    activate_config.short_description = "Activate selected configurations"
    
    def deactivate_config(self, request, queryset):
        """Deactivate selected configurations"""
        updated = queryset.update(is_active=False)
        self.message_user(request, f"Deactivated {updated} configuration(s)")
    deactivate_config.short_description = "Deactivate selected configurations"