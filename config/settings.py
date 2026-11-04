import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
config_path = os.environ.get('COMPLAINT_CONFIG')
LOCAL = json.loads(Path(config_path).read_text()) if config_path else {}


def env(name, default=''):
    return os.environ.get(name, LOCAL.get(name, default))


SECRET_KEY = env('SECRET_KEY')
DATA_KEY = env('DATA_KEY')
if not SECRET_KEY or not DATA_KEY:
    raise RuntimeError('请配置 SECRET_KEY 和 DATA_KEY，参见 config.example.json')
DEBUG = str(env('DEBUG', 'false')).lower() == 'true'
ALLOW_DEMO = DEBUG and str(env('ALLOW_DEMO', 'false')).lower() == 'true'
ALLOWED_HOSTS = str(env('ALLOWED_HOSTS', '127.0.0.1,localhost')).split(',')
CSRF_TRUSTED_ORIGINS = [x for x in str(env('CSRF_TRUSTED_ORIGINS')).split(',') if x]
INSTALLED_APPS = [
    'django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes',
    'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles',
    'desk',
]
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware', 'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware', 'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware', 'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]
ROOT_URLCONF = 'config.urls'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'DIRS': [BASE_DIR / 'templates'],
              'APP_DIRS': True, 'OPTIONS': {'context_processors': [
                  'django.template.context_processors.request', 'django.contrib.auth.context_processors.auth',
                  'django.contrib.messages.context_processors.messages']}}]
WSGI_APPLICATION = 'config.wsgi.application'
DATABASES = {'default': {
    'ENGINE': 'django.db.backends.mysql', 'NAME': env('DB_NAME', 'complaint_desk'),
    'USER': env('DB_USER', 'complaint_app'), 'PASSWORD': env('DB_PASSWORD'),
    'HOST': env('DB_HOST', '127.0.0.1'), 'PORT': env('DB_PORT', '3306'),
    'CONN_MAX_AGE': 60, 'OPTIONS': {'charset': 'utf8mb4', 'init_command': "SET sql_mode='STRICT_TRANS_TABLES'"},
    'TEST': {'CHARSET': 'utf8mb4'},
}}
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = True
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
CHANNEL_CREDENTIAL_ROOT = Path(env('CHANNEL_CREDENTIAL_ROOT', str(
    (Path(config_path).resolve().parent if config_path else Path.home() / '.local/share/wx-complaint-desk')
    / 'channel-credentials')))
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_SSL_REDIRECT = not DEBUG
X_FRAME_OPTIONS = 'DENY'
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
# 仅在由受信任的反向代理独占转发时设置。
if str(env('TRUST_PROXY', 'false')).lower() == 'true':
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
