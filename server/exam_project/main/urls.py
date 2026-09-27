"""
Smart Exam Cell Queue System — URL Routes
All API routes mounted under: /api/v1/main/
"""
from django.urls import path
from .views import *

app_name = 'main'

urlpatterns = [

    # ── Status ────────────────────────────────────────────────────────────────
    path('status/', StatusCheckView.as_view(), name='status'),

    # ── Database ──────────────────────────────────────────────────────────────
    path('db/', DbCheckView.as_view(), name='db_check'),

    # ── Auth (email + password, OTP mandatory) ────────────────────────────────
    path('auth/register/', RegisterView.as_view(), name='auth_register'),
    path('auth/login/', LoginView.as_view(), name='auth_login'),
    path('auth/verify-otp/', VerifyOTPView.as_view(), name='auth_verify_otp'),
    path('auth/resend-otp/', ResendOTPView.as_view(), name='auth_resend_otp'),
    path('auth/forgot-password/', ForgotPasswordView.as_view(), name='auth_forgot_password'),
    path('auth/reset-password/', ResetPasswordView.as_view(), name='auth_reset_password'),
    path('auth/refresh/', RefreshView.as_view(), name='auth_refresh'),
    path('auth/logout/', LogoutView.as_view(), name='auth_logout'),
    path('auth/me/', MeView.as_view(), name='auth_me'),

    # ── Auth (Google) ─────────────────────────────────────────────────────────
    path('auth/google/login/', GoogleLoginView.as_view(), name='auth_google_login'),
    path('auth/google/callback/', GoogleCallbackView.as_view(), name='auth_google_callback'),
    path('auth/google/exchange/', GoogleExchangeView.as_view(), name='auth_google_exchange'),

    # ── Services ──────────────────────────────────────────────────────────────
    path('services/', ServiceListView.as_view(), name='services'),

    # ── Queue ─────────────────────────────────────────────────────────────────
    path('queue/', QueueStateView.as_view(), name='queue_state'),
    path('queue/reset/', QueueResetView.as_view(), name='queue_reset'),

    # ── Tickets ───────────────────────────────────────────────────────────────
    path('tickets/', TicketCreateView.as_view(), name='tickets'),
    path('tickets/mine/', MyTicketView.as_view(), name='my_ticket'),
    path('tickets/<int:ticket_id>/', TicketDetailView.as_view(), name='ticket_detail'),
    path('tickets/<int:ticket_id>/cancel/', TicketCancelView.as_view(), name='ticket_cancel'),

    # ── Appointments ──────────────────────────────────────────────────────────
    path('slots/', SlotAvailabilityView.as_view(), name='slot_availability'),

    # ── Appointments (Staff) ──────────────────────────────────────────────────
    path('appointments/', AppointmentListView.as_view(), name='appointments'),
    path('appointments/<int:ticket_id>/cancel/', AppointmentCancelView.as_view(), name='appointment_cancel'),

    # ── Counters (Staff) ──────────────────────────────────────────────────────
    path('counters/<int:counter_id>/call-next/', CounterCallNextView.as_view(), name='counter_call_next'),
    path('counters/<int:counter_id>/complete/', CounterCompleteView.as_view(), name='counter_complete'),
    path('counters/<int:counter_id>/skip/', CounterSkipView.as_view(), name='counter_skip'),
    path('counters/<int:counter_id>/transfer/', CounterTransferView.as_view(), name='counter_transfer'),
]
