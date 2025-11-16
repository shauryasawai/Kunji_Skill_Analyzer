from django import forms
from .models import JobDescription

class JDUploadForm(forms.ModelForm):
    domain = forms.ChoiceField(
        choices=[
            ('', 'Auto-detect'),
            ('Technical', 'Technical'),
            ('Marketing', 'Marketing'),
            ('Finance', 'Finance'),
            ('HR', 'Human Resources'),
            ('Sales', 'Sales'),
            ('Operations', 'Operations'),
        ],
        required=False,
        widget=forms.Select(attrs={'class': 'form-select'})
    )
    
    class Meta:
        model = JobDescription
        fields = ['title', 'file']
        widgets = {
            'title': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., Senior Marketing Manager'}),
            'file': forms.FileInput(attrs={'class': 'form-control', 'accept': '.txt,.pdf,.docx'}),
        }

class CandidateMatchForm(forms.Form):
    min_match_percentage = forms.IntegerField(
        min_value=0, 
        max_value=100, 
        initial=50,
        label="Minimum Match Percentage"
    )
    use_fuzzy_matching = forms.BooleanField(
        initial=True,
        required=False,
        label="Use Fuzzy Matching"
    )
    fuzzy_threshold = forms.IntegerField(
        min_value=50,
        max_value=100,
        initial=85,
        required=False,
        label="Fuzzy Match Threshold (%)"
    )