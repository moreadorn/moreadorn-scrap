from django.urls import path
from . import views

urlpatterns = [
    path('', views.scrape_leads, name='scrape_leads'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('change-password/', views.change_password, name='change_password'),
    path('change-email/', views.change_email, name='change_email'),
    # Page 1 scraping
    path('scrape/', views.start_scrape, name='start_scrape'),
    path('status/<str:job_id>/', views.job_status, name='job_status'),
    path('download/<str:job_id>/', views.download_csv, name='download_csv'),
    # Page 2 - email template
    path('email-content/', views.email_content, name='email_content'),
    path('email-content/delete/<int:pk>/', views.delete_email_template, name='delete_email_template'),
    path('email-content/preview/<str:pk>/', views.preview_email_template, name='preview_email_template'),
    path('email-content/design/delete/<int:pk>/', views.delete_email_design, name='delete_email_design'),
    path('email-content/design/preview/<int:pk>/', views.preview_email_design, name='preview_email_design'),
    path('ai-generate-email/', views.ai_generate_email, name='ai_generate_email'),
    path('ai-format-email/', views.ai_format_email, name='ai_format_email'),
    path('ai-save-template/', views.ai_save_template, name='ai_save_template'),
    path('ai-template/delete/<int:pk>/', views.delete_ai_template, name='delete_ai_template'),
    path('ai-template/preview/<int:pk>/', views.preview_ai_template, name='preview_ai_template'),
    path('custom-template/delete/<int:pk>/', views.delete_custom_template, name='delete_custom_template'),
    path('custom-template/preview/<int:pk>/', views.preview_custom_template, name='preview_custom_template'),
    # API Keys
    path('api-keys/', views.api_keys_page, name='api_keys'),
    path('api-keys/add/', views.add_api_key, name='add_api_key'),
    path('api-keys/activate/<int:pk>/', views.activate_api_key, name='activate_api_key'),
    path('api-keys/deactivate/<int:pk>/', views.deactivate_api_key, name='deactivate_api_key'),
    path('api-keys/delete/<int:pk>/', views.delete_api_key, name='delete_api_key'),
    # WhatsApp API config
    path('whatsapp-api/', views.whatsapp_config_page, name='whatsapp_config'),
    path('whatsapp-api/add/', views.add_whatsapp_config, name='add_whatsapp_config'),
    path('whatsapp-api/activate/<int:pk>/', views.activate_whatsapp_config, name='activate_whatsapp_config'),
    path('whatsapp-api/deactivate/<int:pk>/', views.deactivate_whatsapp_config, name='deactivate_whatsapp_config'),
    path('whatsapp-api/delete/<int:pk>/', views.delete_whatsapp_config, name='delete_whatsapp_config'),
    # Page 3 - whatsapp template
    path('wa-content/', views.wa_content, name='wa_content'),
    path('wa-content/delete/<int:pk>/', views.delete_wa_template, name='delete_wa_template'),
    # Page 4 - send email
    path('send-email/', views.send_email_page, name='send_email'),
    path('send-email/do/', views.do_send_email, name='do_send_email'),
    # Page 5 - send whatsapp
    path('send-whatsapp/', views.send_whatsapp_page, name='send_whatsapp'),
    path('send-whatsapp/do/', views.do_send_whatsapp, name='do_send_whatsapp'),
    # Page 6 - sent leads
    path('sent-leads/', views.sent_leads, name='sent_leads'),
    path('sent-leads/delete/', views.delete_leads, name='delete_leads'),
    path('leads/delete-all/', views.delete_all_leads, name='delete_all_leads'),
    path('waste-leads/', views.waste_leads, name='waste_leads'),
    path('waste-leads/delete/', views.delete_waste_leads, name='delete_waste_leads'),
    path('health/', views.health_check, name='health_check'),
    path('api/dummy-lead/', views.create_dummy_lead, name='create_dummy_lead'),
    path('api/dummy-lead-2/', views.create_dummy_lead_2, name='create_dummy_lead_2'),
    path('sent-leads/resend-email/', views.resend_email_view, name='resend_email'),
    path('sent-leads/resend-whatsapp/', views.resend_whatsapp_view, name='resend_whatsapp'),
]
