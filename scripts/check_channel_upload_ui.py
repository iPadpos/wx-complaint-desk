"""本地上传验收：仅使用临时生成的证书和停用渠道，不请求真实微信。"""
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
    parser.add_argument('--config', default=os.environ.get('COMPLAINT_CONFIG'))
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    base = args.url.rstrip('/')
    if urlsplit(base).hostname not in ('127.0.0.1', 'localhost') or not args.config:
        raise SystemExit('仅允许本地验收，且必须指定本地配置文件')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.environ['COMPLAINT_CONFIG'] = args.config
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    os.environ['DJANGO_ALLOW_ASYNC_UNSAFE'] = 'true'
    import django
    django.setup()
    from django.conf import settings
    from django.db.models import Q
    from django.utils import timezone
    from desk.models import WechatChannel

    password = json.loads(Path(args.credentials).read_text())['admin_password']
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    name = '上传页面验收-' + secrets.token_hex(3)
    mch_id = str(1960000000 + secrets.randbelow(1000000))
    first_key, second_key = secrets.token_hex(16), secrets.token_hex(16)
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, mch_id)])
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(private.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(timezone.now() - timedelta(days=1)).not_valid_after(timezone.now() + timedelta(days=30))
        .sign(private, hashes.SHA256()))
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
    private_pem = private.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    public_pem = public_private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    checks, errors, directories = [], [], set()

    def passed(text):
        checks.append({'name': text, 'result': 'passed'})
        print('PASS', text, flush=True)

    def remember_paths(channel):
        for value in (channel.private_key_path, channel.public_key_path, channel.secondary_key_path):
            if value and Path(value).parent.name.startswith(mch_id + '-'):
                directories.add(Path(value).parent)

    with sync_playwright() as p:
        executable = os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
        browser = p.chromium.launch(headless=True, **({'executable_path': executable} if executable else {}))
        page = browser.new_page(viewport={'width': 1440, 'height': 1080})
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            page.goto(base + '/channels/new/')
            page.get_by_label('账号', exact=False).fill('admin')
            page.get_by_label('密码', exact=False).fill(password)
            page.get_by_role('button', name='登录', exact=True).click()
            expect(page.get_by_role('heading', name='新增渠道账号', exact=True)).to_be_visible()
            expect(page.locator('[data-api-key-status]')).to_have_text('未配置')
            expect(page.locator('[data-file-status]')).to_have_text(['未配置'] * 4)
            page.get_by_label('渠道账号名称', exact=False).fill(name)
            page.get_by_label('微信渠道号', exact=False).fill(mch_id)
            page.get_by_label('启用渠道调用', exact=False).uncheck()
            page.get_by_label('APIv3 密钥', exact=False).fill(first_key)
            page.get_by_label('渠道证书序列号', exact=False).fill('PUB_KEY_ID_WRONG_POSITION')
            page.get_by_label('微信公钥 ID', exact=False).fill('PUB_KEY_ID_UI_UPLOAD_TEST')
            expect(page.locator('[name=certificate_bundle]')).to_have_count(0)
            page.get_by_label('上传渠道 API 证书 PEM', exact=False).set_input_files(
                {'name': 'bad.pem', 'mimeType': 'application/x-pem-file', 'buffer': b'not a certificate'})
            page.get_by_role('button', name='保存渠道账号', exact=True).click()
            expect(page.get_by_role('alert')).to_contain_text('保存未成功')
            expect(page.get_by_label('APIv3 密钥', exact=False)).to_have_value('')
            expect(page.locator('[data-api-key-status]')).to_have_text('未配置')
            assert not WechatChannel.objects.filter(mch_id=mch_id).exists()
            assert first_key not in page.content()
            passed('无效上传明确报错，不保存渠道且不回显输入密钥')

            page.get_by_label('APIv3 密钥', exact=False).fill(first_key)
            page.get_by_label('上传渠道 API 证书 PEM', exact=False).set_input_files(
                {'name': 'apiclient_cert.pem', 'mimeType': 'application/x-pem-file', 'buffer': certificate_pem})
            page.get_by_label('上传渠道私钥 PEM', exact=False).set_input_files(
                {'name': 'apiclient_key.pem', 'mimeType': 'application/x-pem-file', 'buffer': private_pem})
            certificate_picker = page.locator('[data-credential-upload]').filter(has=page.locator('[name=certificate_upload]'))
            expect(certificate_picker.locator('[data-file-name]')).to_have_text('apiclient_cert.pem')
            certificate_picker.get_by_role('button', name='移除本次选择', exact=True).click()
            expect(certificate_picker.locator('[data-file-name]')).to_have_text('未选择文件')
            expect(page.get_by_label('上传渠道 API 证书 PEM', exact=False)).to_have_value('')
            page.get_by_label('上传渠道 API 证书 PEM', exact=False).set_input_files(
                {'name': 'apiclient_cert.pem', 'mimeType': 'application/x-pem-file', 'buffer': certificate_pem})
            passed('文件选择卡片显示名称，支持移除并重新选择')
            page.get_by_label('上传微信公钥／平台证书 PEM', exact=False).set_input_files(
                {'name': 'public.pem', 'mimeType': 'application/x-pem-file', 'buffer': public_pem})
            page.get_by_role('heading', name='API 证书与私钥', exact=True).scroll_into_view_if_needed()
            page.screenshot(path=str(out / '选择PEM文件.png'), full_page=True)
            page.get_by_role('button', name='保存渠道账号', exact=True).click()
            row = page.locator('tr').filter(has_text=name)
            expect(row).to_contain_text('已保存')
            channel = WechatChannel.objects.get(mch_id=mch_id)
            remember_paths(channel)
            assert channel.api_v3_key == first_key and not channel.enabled
            assert channel.serial_no == format(certificate.serial_number, 'X')
            assert Path(channel.private_key_path).stat().st_mode & 0o777 == 0o600
            original_paths = (channel.private_key_path, channel.public_key_path)
            passed('分别上传证书PEM、私钥PEM和微信公钥，自动读取序列号并保存受限文件')

            row.get_by_role('link', name='编辑', exact=True).click()
            expect(page.locator('[data-api-key-status]')).to_have_text('已保存')
            expect(page.get_by_label('APIv3 密钥', exact=False)).to_have_value('')
            expect(page.get_by_label('渠道证书序列号', exact=False)).to_have_value(channel.serial_no)
            assert first_key not in page.content()
            for field, filename in [('certificate_upload', 'API 证书信息已保存'),
                                    ('private_key_upload', 'apiclient_key.pem'),
                                    ('public_key_upload', 'wechatpay_verification.pem')]:
                picker = page.locator('[data-credential-upload]').filter(has=page.locator('[name=' + field + ']'))
                expect(picker.locator('[data-file-status]')).to_have_text('已配置')
                expect(picker.locator('[data-file-name]')).to_have_text(filename)
            private_picker = page.locator('[data-credential-upload]').filter(has=page.locator('[name=private_key_upload]'))
            page.locator('[name=private_key_upload]').set_input_files(
                {'name': 'replacement.pem', 'mimeType': 'application/x-pem-file', 'buffer': private_pem})
            expect(private_picker.locator('[data-file-status]')).to_have_text('待保存')
            private_picker.get_by_role('button', name='移除本次选择').click()
            expect(private_picker.locator('[data-file-status]')).to_have_text('已配置')
            expect(private_picker.locator('[data-file-name]')).to_have_text('apiclient_key.pem')
            passed('渠道已保存材料逐项标识，替换待保存，移除后恢复原文件标识')
            page.screenshot(path=str(out / '上传后编辑.png'), full_page=True)
            page.get_by_role('button', name='保存渠道账号', exact=True).click()
            channel.refresh_from_db()
            assert channel.api_v3_key == first_key
            assert (channel.private_key_path, channel.public_key_path) == original_paths
            passed('编辑显示密钥已保存，留空提交保留密钥和已上传文件')

            page.locator('tr').filter(has_text=name).get_by_role('link', name='编辑', exact=True).click()
            page.get_by_label('APIv3 密钥', exact=False).fill(second_key)
            page.get_by_role('button', name='保存渠道账号', exact=True).click()
            channel.refresh_from_db()
            assert channel.api_v3_key == second_key
            page.locator('tr').filter(has_text=name).get_by_role('link', name='编辑', exact=True).click()
            assert first_key not in page.content() and second_key not in page.content()
            passed('替换APIv3密钥实际持久保存，编辑页继续不回显')

            page.set_viewport_size({'width': 390, 'height': 844})
            page.get_by_label('上传微信公钥／平台证书 PEM', exact=False).scroll_into_view_if_needed()
            width = page.evaluate('({scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth})')
            assert width['scroll'] <= width['client'], width
            page.screenshot(path=str(out / '手机上传表单.png'), full_page=True)
            passed('390px上传表单无横向溢出')
            page.set_viewport_size({'width': 1440, 'height': 1080})
            page.get_by_role('link', name='返回渠道列表', exact=True).click()
            page.locator('tr').filter(has_text=name).get_by_role('link', name='删除', exact=True).click()
            page.get_by_role('button', name='确认删除渠道账号', exact=True).click()
            expect(page.locator('tr').filter(has_text=name)).to_have_count(0)
            passed('临时停用渠道已通过页面删除')
            assert not errors, errors
            passed('上传页面无JavaScript错误')
        except Exception:
            page.screenshot(path=str(out / '失败现场.png'), full_page=True)
            raise
        finally:
            browser.close()
            for channel in WechatChannel.objects.filter(mch_id=mch_id, name=name):
                remember_paths(channel)
                channel.delete()
            for directory in directories:
                assert directory.parent.resolve() == Path(settings.CHANNEL_CREDENTIAL_ROOT).resolve()
                assert directory.name.startswith(mch_id + '-')
                assert not WechatChannel.objects.filter(Q(private_key_path__startswith=str(directory) + '/') |
                    Q(public_key_path__startswith=str(directory) + '/') | Q(secondary_key_path__startswith=str(directory) + '/')).exists()
                shutil.rmtree(directory)
            (out / 'ui-results.json').write_text(json.dumps({'checks': checks, 'javascript_errors': errors,
                'browser_closed': True, 'temporary_materials_removed': True,
                'notes': '仅生成测试证书并保存停用渠道；未使用真实凭据、未调用真实微信。'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
