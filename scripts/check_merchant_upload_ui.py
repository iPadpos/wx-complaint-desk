"""仅本地验收商户凭据表单，使用合成材料和停用商户，不请求微信。"""
import argparse
import json
import os
import secrets
import shutil
import sys
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8316')
    parser.add_argument('--credentials', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    base = args.url.rstrip('/')
    if urlsplit(base).hostname not in ('127.0.0.1', 'localhost'):
        raise SystemExit('仅允许本地验收')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.environ['COMPLAINT_CONFIG'] = args.config
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    os.environ['DJANGO_ALLOW_ASYNC_UNSAFE'] = 'true'
    import django
    django.setup()
    from django.conf import settings
    from django.db.models import Q
    from django.utils import timezone
    from desk.models import Merchant

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    name = '商户上传验收-' + secrets.token_hex(3)
    mch_id = str(1950000000 + secrets.randbelow(1000000))
    api_key = secrets.token_hex(16)
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, mch_id)])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(private.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(timezone.now() - timedelta(days=1)).not_valid_after(timezone.now() + timedelta(days=30))
        .sign(private, hashes.SHA256()))
    serial = format(cert.serial_number, 'X')
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    private_pem = private.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    public_pem = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    checks, errors, requests, directories = [], [], [], set()

    def passed(label):
        checks.append(label)
        print('PASS', label, flush=True)

    def remember(merchant):
        for value in (merchant.private_key_path, merchant.public_key_path, merchant.secondary_key_path):
            if value and Path(value).parent.name.startswith(mch_id + '-'):
                directories.add(Path(value).parent)

    def select(input, filename, raw):
        input.set_input_files({'name': filename, 'mimeType': 'application/x-pem-file', 'buffer': raw})

    with sync_playwright() as p:
        executable = os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
        browser = p.chromium.launch(headless=True, **({'executable_path': executable} if executable else {}))
        page = browser.new_page(viewport={'width': 1440, 'height': 1080})
        page.on('pageerror', lambda err: errors.append(str(err)))
        try:
            page.goto(base + '/merchants/new/')
            page.get_by_label('账号', exact=False).fill('admin')
            page.get_by_label('密码', exact=False).fill(json.loads(Path(args.credentials).read_text())['admin_password'])
            page.get_by_role('button', name='登录', exact=True).click()
            expect(page.get_by_role('heading', name='新增商户', exact=True)).to_be_visible()
            expect(page.locator('[data-api-key-status]')).to_have_text('未配置')
            expect(page.locator('[data-channel-fields]')).to_be_hidden()
            expect(page.locator('[name=private_key_path]')).to_be_hidden()
            expect(page.locator('[name=certificate_bundle]')).to_have_count(0)
            expect(page.locator('[name=certificate_upload]')).to_be_visible()
            expect(page.locator('[data-file-status]')).to_have_text(['未配置'] * 4)
            assert page.locator('form[data-merchant-config]').get_attribute('enctype') == 'multipart/form-data'
            passed('新增商户使用新版文件卡片、密钥状态和折叠路径')

            page.get_by_label('商户名称', exact=False).fill(name)
            page.get_by_label('微信商户号', exact=False).fill(mch_id)
            page.get_by_label('启用同步', exact=False).uncheck()
            page.get_by_label('APIv3 密钥', exact=False).fill(api_key)
            public_id = page.locator('[name=public_key_id]')
            public_id.fill('PUB_KEY_ID_MERCHANT_UI_TEST')
            page.wait_for_load_state('networkidle')
            page.on('request', lambda request: requests.append(request.url))
            count_before = len(requests)
            select(page.locator('[name=certificate_upload]'), 'apiclient_cert.pem', cert_pem)
            expect(page.locator('[name=serial_no]')).to_have_value(serial)
            expect(page.locator('[data-credential-upload]').filter(has=page.locator('[name=certificate_upload]')).locator('[data-file-status]')).to_have_text('待保存')
            select(page.locator('[name=private_key_upload]'), 'apiclient_key.pem', private_pem)
            select(page.locator('[name=public_key_upload]'), 'wechatpay.pem', cert_pem)
            expect(public_id).to_have_value(serial)
            select(page.locator('[name=public_key_upload]'), 'pub_key.pem', public_pem)
            expect(public_id).to_have_value('PUB_KEY_ID_MERCHANT_UI_TEST')
            assert len(requests) == count_before
            passed('证书即时预填、普通公钥保留手填ID，选择文件不发送请求')

            page.screenshot(path=str(out / '新增商户选择PEM.png'), full_page=True)
            page.get_by_role('button', name='保存商户', exact=True).click()
            row = page.locator('tr').filter(has_text=name)
            expect(row).to_contain_text('商户自行调用')
            expect(row).to_contain_text('已停用')
            merchant = Merchant.objects.get(mch_id=mch_id)
            remember(merchant)
            assert merchant.api_v3_key == api_key and merchant.serial_no == serial and not merchant.enabled
            assert Path(merchant.private_key_path).stat().st_mode & 0o777 == 0o600
            original_paths = (merchant.private_key_path, merchant.public_key_path)
            passed('通过页面上传三份PEM并保存为停用的自行调用商户')

            row.get_by_role('link', name='编辑', exact=True).click()
            expect(page.locator('[data-api-key-status]')).to_have_text('已保存')
            expect(page.locator('[name=api_v3_key_input]')).to_have_value('')
            assert api_key not in page.content()
            for field, filename in [('certificate_upload', 'API 证书信息已保存'),
                                    ('private_key_upload', 'apiclient_key.pem'),
                                    ('public_key_upload', 'wechatpay_verification.pem')]:
                picker = page.locator('[data-credential-upload]').filter(has=page.locator('[name=' + field + ']'))
                expect(picker.locator('[data-file-status]')).to_have_text('已配置')
                expect(picker.locator('[data-file-name]')).to_have_text(filename)
            private_picker = page.locator('[data-credential-upload]').filter(has=page.locator('[name=private_key_upload]'))
            select(page.locator('[name=private_key_upload]'), 'replacement.pem', private_pem)
            expect(private_picker.locator('[data-file-status]')).to_have_text('待保存')
            expect(private_picker.locator('[data-file-caption]')).to_contain_text('保存后替换')
            private_picker.get_by_role('button', name='移除本次选择').click()
            expect(private_picker.locator('[data-file-status]')).to_have_text('已配置')
            expect(private_picker.locator('[data-file-name]')).to_have_text('apiclient_key.pem')
            passed('商户已保存材料逐项标识，替换待保存，移除后恢复原文件标识')
            page.get_by_role('button', name='保存商户', exact=True).click()
            merchant.refresh_from_db()
            assert merchant.api_v3_key == api_key
            assert (merchant.private_key_path, merchant.public_key_path) == original_paths
            passed('编辑显示已保存且不回显，留空提交保留密钥和文件')

            page.locator('tr').filter(has_text=name).get_by_role('link', name='编辑', exact=True).click()
            page.get_by_label('接入方式', exact=False).select_option('channel')
            expect(page.locator('[data-direct-credentials]')).to_be_hidden()
            expect(page.locator('[name=certificate_upload]')).to_be_disabled()
            expect(page.locator('[data-channel-fields]')).to_be_visible()
            page.get_by_label('接入方式', exact=False).select_option('direct')
            expect(page.locator('[data-channel-fields]')).to_be_hidden()
            expect(page.locator('[name=certificate_upload]')).to_be_enabled()
            expect(page.locator('[name=serial_no]')).to_have_value(serial)
            passed('代调用隐藏并禁用商户凭据，切回自行调用保留原填写')

            page.locator('[name=api_v3_key_input]').fill('bad')
            page.get_by_role('button', name='保存商户', exact=True).click()
            expect(page.get_by_role('alert')).to_contain_text('保存未成功')
            expect(page.locator('[name=api_v3_key_input]')).to_have_value('')
            expect(page.locator('[data-api-key-status]')).to_have_text('已保存')
            expect(page.locator('[data-credential-upload]').filter(has=page.locator('[name=private_key_upload]')).locator('[data-file-status]')).to_have_text('已配置')
            merchant.refresh_from_db()
            assert merchant.api_v3_key == api_key
            passed('错误输入明确提示并保留已保存凭据')

            page.set_viewport_size({'width': 390, 'height': 844})
            page.locator('[name=certificate_upload]').scroll_into_view_if_needed()
            assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
            page.screenshot(path=str(out / '手机商户凭据表单.png'), full_page=True)
            passed('手机390px凭据表单无横向溢出')
            page.set_viewport_size({'width': 1440, 'height': 1080})
            page.get_by_role('link', name='返回列表', exact=True).click()
            page.locator('tr').filter(has_text=name).get_by_role('link', name='删除', exact=True).click()
            page.get_by_role('button', name='确认删除商户', exact=True).click()
            expect(page.locator('tr').filter(has_text=name)).to_have_count(0)
            assert not errors, errors
            passed('临时商户已通过页面删除，页面无JavaScript错误')
        except Exception:
            page.screenshot(path=str(out / '失败现场.png'), full_page=True)
            raise
        finally:
            browser.close()
            for merchant in Merchant.objects.filter(mch_id=mch_id, name=name):
                remember(merchant)
                merchant.delete()
            for directory in directories:
                assert directory.parent.resolve() == Path(settings.CHANNEL_CREDENTIAL_ROOT).resolve()
                assert not Merchant.objects.filter(Q(private_key_path__startswith=str(directory) + '/') |
                    Q(public_key_path__startswith=str(directory) + '/') | Q(secondary_key_path__startswith=str(directory) + '/')).exists()
                shutil.rmtree(directory)
            (out / 'ui-results.json').write_text(json.dumps({'checks': checks, 'javascript_errors': errors,
                'real_wechat_called': False, 'temporary_merchant_removed': True}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
