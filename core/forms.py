from django import forms
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError


class ClientRegistrationForm(forms.Form):
    # ── Крок 1: Компанія ──────────────────────────────────
    company_name = forms.CharField(
        max_length=200, label='Назва компанії',
        widget=forms.TextInput(attrs={'placeholder': 'Müller GmbH'}))
    company_country = forms.ChoiceField(
        choices=[
            ('DE', 'Німеччина'), ('AT', 'Австрія'),
            ('CH', 'Швейцарія'), ('UA', 'Україна'), ('OTHER', 'Інша'),
        ],
        initial='DE', label='Країна')
    contact_phone = forms.CharField(
        max_length=50, required=False, label='Телефон',
        widget=forms.TextInput(attrs={'placeholder': '+49 123 456789'}))

    # ── Крок 2: Пакет ──────────────────────────────────────
    package = forms.ChoiceField(
        choices=[
            ('free',     'Free — безкоштовно'),
            ('starter',  'Starter — €30/міс'),
            ('business', 'Business — €60/міс'),
        ],
        initial='starter', label='Пакет')

    # ── Крок 3: Акаунт ──────────────────────────────────────
    owner_name = forms.CharField(
        max_length=200, label="Ваше ім'я",
        widget=forms.TextInput(attrs={'placeholder': 'Hans Müller'}))
    email = forms.EmailField(
        label='Email',
        widget=forms.EmailInput(attrs={'placeholder': 'hans@firma.de'}))
    password1 = forms.CharField(
        widget=forms.PasswordInput(), label='Пароль', min_length=8)
    password2 = forms.CharField(
        widget=forms.PasswordInput(), label='Підтвердіть пароль')

    # ── Згода ───────────────────────────────────────────────
    agree_terms = forms.BooleanField(
        required=True,
        label='Я погоджуюсь з умовами використання')

    def clean_email(self):
        email = self.cleaned_data['email'].lower().strip()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError(
                'Акаунт з таким email вже існує. '
                'Спробуйте увійти або скиньте пароль.')
        return email

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get('password1')
        p2 = cleaned.get('password2')
        if p1 and p2 and p1 != p2:
            raise ValidationError({'password2': 'Паролі не співпадають.'})
        return cleaned
