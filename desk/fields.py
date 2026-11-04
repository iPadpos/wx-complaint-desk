import json
from cryptography.fernet import Fernet
from django.conf import settings
from django.db import models


class EncryptedJSONField(models.TextField):
    """密钥和投诉敏感内容在落库时加密；DATA_KEY 只由部署环境提供。"""
    def from_db_value(self, value, expression, connection):
        if value is None:
            return None
        return json.loads(Fernet(settings.DATA_KEY.encode()).decrypt(value.encode()))

    def get_prep_value(self, value):
        if value is None:
            return None
        return Fernet(settings.DATA_KEY.encode()).encrypt(
            json.dumps(value, ensure_ascii=False).encode()).decode()
