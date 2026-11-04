"""本地浏览器验收：选择证书即时预填，不保存渠道、不发送文件请求。"""
import argparse
import json
import os
from datetime import datetime, timedelta, timezone
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
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    base = args.url.rstrip('/')
    if urlsplit(base).hostname not in ('127.0.0.1', 'localhost'):
        raise SystemExit('仅允许本地验收')
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, '1900000001')])

    def certificate(serial):
        return (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(key.public_key()).serial_number(serial)
                .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
                .not_valid_after(datetime.now(timezone.utc) + timedelta(days=30))
                .sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM))

    first_serial, next_serial = 'ABC123', '80000123456789ABCDEF'
    first_pem, next_pem = certificate(int(first_serial, 16)), certificate(int(next_serial, 16))
    public_pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    checks, errors, requests = [], [], []

    def passed(name):
        checks.append(name)
        print('PASS', name, flush=True)

    def select(input, pem, name='certificate.pem'):
        input.set_input_files({'name': name, 'mimeType': 'application/x-pem-file', 'buffer': pem})

    with sync_playwright() as p:
        executable = os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
        browser = p.chromium.launch(headless=True, **({'executable_path': executable} if executable else {}))
        page = browser.new_page(viewport={'width': 1440, 'height': 1080})
        page.on('pageerror', lambda err: errors.append(str(err)))
        try:
            page.goto(base + '/channels/new/')
            page.get_by_label('账号', exact=False).fill('admin')
            page.get_by_label('密码', exact=False).fill(json.loads(Path(args.credentials).read_text())['admin_password'])
            page.get_by_role('button', name='登录', exact=True).click()
            expect(page.get_by_role('heading', name='新增渠道账号', exact=True)).to_be_visible()
            page.wait_for_load_state('networkidle')
            page.on('request', lambda request: requests.append({'method': request.method, 'url': request.url}))
            cert_input = page.locator('[name=certificate_upload]')
            serial = page.locator('[name=serial_no]')
            picker = page.locator('[data-credential-upload]').filter(has=cert_input)
            status = picker.locator('[data-certificate-preview]')
            serial.fill('OLD_SERIAL')
            select(cert_input, first_pem)
            expect(serial).to_have_value(first_serial)
            expect(status).to_contain_text('已读取并填入')
            expect(page.locator('[name=mch_id]')).to_have_value('')
            passed('未填写渠道号、未保存时，选中文件立即填入序列号')
            picker.get_by_role('button', name='移除本次选择').click()
            expect(serial).to_have_value('OLD_SERIAL')
            expect(status).to_be_hidden()
            passed('移除所选证书恢复原填写值')

            select(cert_input, first_pem)
            expect(serial).to_have_value(first_serial)
            select(cert_input, next_pem)
            expect(serial).to_have_value(next_serial)
            passed('替换证书立即更新，正确处理十六进制前导补零')
            for raw in (b'not a certificate', b'', b'x' * (64 * 1024 + 1),
                        b'-----BEGIN CERTIFICATE-----\nMAA=\n-----END CERTIFICATE-----\n'):
                select(cert_input, raw, 'bad.pem')
                expect(status).to_contain_text('未能读取')
                expect(serial).to_have_value('OLD_SERIAL')
            passed('损坏、空文件和超限文件提示失败并保留原值')

            public_input = page.locator('[name=public_key_upload]')
            public_id = page.locator('[name=public_key_id]')
            public_picker = page.locator('[data-credential-upload]').filter(has=public_input)
            public_id.fill('PUB_KEY_ID_PREVIEW_TEST')
            select(public_input, next_pem, 'wechatpay.pem')
            expect(public_id).to_have_value(next_serial)
            select(public_input, public_pem, 'pub_key.pem')
            expect(public_id).to_have_value('PUB_KEY_ID_PREVIEW_TEST')
            expect(public_picker.locator('[data-certificate-preview]')).to_contain_text('公钥文件不含公钥 ID')
            public_id.fill('')
            select(public_input, public_pem, 'pub_key.pem')
            expect(public_id).to_have_value('')
            passed('平台证书即时填入序列号，普通公钥不伪造ID且保留手动值')

            page.get_by_text('备用验签材料', exact=False).click()
            select(page.locator('[name=secondary_key_upload]'), first_pem)
            expect(page.locator('[name=secondary_key_id]')).to_have_value(first_serial)
            passed('备用平台证书填入对应备用字段')

            picker.get_by_role('button', name='移除本次选择').click()
            page.evaluate('''() => {
              window.originalFileText = File.prototype.text;
              File.prototype.text = function() {
                if (this.name === 'slow.pem') {
                  return new Promise(resolve => { window.finishFileRead = () => window.originalFileText.call(this).then(resolve); });
                }
                return window.originalFileText.call(this);
              };
            }''')
            select(cert_input, first_pem, 'slow.pem')
            expect(status).to_contain_text('正在读取')
            select(cert_input, next_pem)
            expect(serial).to_have_value(next_serial)
            page.evaluate('window.finishFileRead()')
            expect(serial).to_have_value(next_serial)
            passed('快速重选时，较早的读取结果不会覆盖新证书')

            select(cert_input, first_pem, 'slow.pem')
            expect(status).to_contain_text('正在读取')
            serial.fill('MANUAL_SERIAL')
            page.evaluate('window.finishFileRead()')
            expect(status).to_contain_text('手动修改')
            expect(serial).to_have_value('MANUAL_SERIAL')
            picker.get_by_role('button', name='移除本次选择').click()
            expect(serial).to_have_value('MANUAL_SERIAL')
            passed('读取期间的手动填写和移除后的手动值保留')

            select(cert_input, next_pem, 'slow.pem')
            expect(status).to_contain_text('正在读取')
            picker.get_by_role('button', name='移除本次选择').click()
            page.evaluate('window.finishFileRead()')
            expect(serial).to_have_value('MANUAL_SERIAL')
            expect(status).to_be_hidden()
            page.evaluate('() => { File.prototype.text = window.originalFileText; }')
            passed('读取期间移除文件会取消过期结果')

            select(cert_input, next_pem, 'apiclient_cert.pem')
            expect(serial).to_have_value(next_serial)
            page.screenshot(path=str(out / '选择证书立即填入.png'), full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            serial.scroll_into_view_if_needed()
            assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
            page.screenshot(path=str(out / '手机即时读取.png'), full_page=True)
            assert not requests, requests
            assert not errors, errors
            passed('桌面及手机显示正常，选文件不产生网络请求或JavaScript错误')
        except Exception:
            page.screenshot(path=str(out / '失败现场.png'), full_page=True)
            raise
        finally:
            browser.close()
            (out / 'ui-results.json').write_text(json.dumps({'checks': checks, 'javascript_errors': errors,
                'selection_requests': requests, 'channel_saved': False, 'real_wechat_called': False}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
