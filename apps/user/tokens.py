"""用户激活 token 的生成与校验。"""

import hashlib

from django.conf import settings
from itsdangerous import URLSafeTimedSerializer


ACTIVATION_SALT = "e-commerce.user.activation"


def _serializer():
    """使用项目密钥和独立 salt 创建带时间戳的签名序列化器。"""
    return URLSafeTimedSerializer(
        settings.SECRET_KEY,
        salt=ACTIVATION_SALT,
        signer_kwargs={"digest_method": hashlib.sha256},
    )


def make_activation_token(user):
    """把用户身份信息封装进带时间戳的 itsdangerous token。"""
    return _serializer().dumps(
        {
            "action": "activate",
            "uid": user.pk,
            "email": user.email,
        }
    )


def load_activation_token(token):
    """校验签名与有效期并返回 token 中的身份信息。"""
    return _serializer().loads(
        token,
        max_age=settings.USER_ACTIVATION_TIMEOUT,
    )
