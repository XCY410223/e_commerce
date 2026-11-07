"""用户相关的 Celery 异步任务。"""

import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse

from user.models import User
from user.tokens import make_activation_token

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name="user.send_activation_email",
    max_retries=3,
    default_retry_delay=60,
)
def send_activation_email(self, user_id):
    """生成激活链接并通过邮件发送；邮件异常时由 Celery 重试。"""
    user = User.objects.filter(pk=user_id, is_active=False).first()
    if user is None:
        return {"sent": False, "reason": "user_not_found_or_active"}
    if not user.email:
        return {"sent": False, "reason": "email_missing"}

    token = make_activation_token(user)
    activation_path = reverse("user:activate", kwargs={"token": token})
    activation_url = f"{settings.SITE_URL.rstrip('/')}{activation_path}"
    context = {
        "username": user.username,
        "activation_url": activation_url,
        "expire_hours": max(1, settings.USER_ACTIVATION_TIMEOUT // 3600),
    }

    subject = "激活您的天天生鲜账号"
    text_body = render_to_string("user/emails/activation.txt", context)
    html_body = render_to_string("user/emails/activation.html", context)
    message = EmailMultiAlternatives(
        subject,
        text_body,
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
    )
    message.attach_alternative(html_body, "text/html")

    try:
        message.send()
    except Exception as exc:
        logger.exception("激活邮件发送失败 user_id=%s", user.pk)
        raise self.retry(exc=exc)

    return {"sent": True, "user_id": user.pk, "email": user.email}
