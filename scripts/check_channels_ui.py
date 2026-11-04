"""仅在本地创建无微信凭据的临时渠道与商户，验收后通过页面删除。"""
import argparse
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8316')
    parser.add_argument('--credentials', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    base = args.url.rstrip('/')
    if urlsplit(base).hostname not in ('127.0.0.1', 'localhost'):
        raise SystemExit('渠道页面验收仅允许本地环境')
    password = json.loads(Path(args.credentials).read_text())['admin_password']
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    checks, errors = [], []
    token = secrets.token_hex(3)
    channel_name = '页面验收渠道-' + token
    merchant_name = '页面验收子商户-' + token
    channel_mch = str(1980000000 + secrets.randbelow(1000000))
    merchant_mch = str(1970000000 + secrets.randbelow(1000000))

    def passed(name):
        checks.append({'name': name, 'result': 'passed'})
        print('PASS', name, flush=True)

    with sync_playwright() as p:
        executable = os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
        browser = p.chromium.launch(headless=True, **({'executable_path': executable} if executable else {}))
        page = browser.new_page(viewport={'width': 1440, 'height': 1080})
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            page.goto(base + '/channels/')
            page.get_by_label('账号', exact=False).fill('admin')
            page.get_by_label('密码', exact=False).fill(password)
            page.get_by_role('button', name='登录', exact=True).click()
            expect(page.get_by_role('heading', name='渠道账号', exact=True)).to_be_visible()
            page.get_by_role('link', name='新增渠道账号', exact=False).click()
            page.get_by_label('渠道账号名称', exact=False).fill(channel_name)
            page.get_by_label('所属支付机构', exact=False).fill('随行付（页面验收）')
            page.get_by_label('微信渠道号', exact=False).fill(channel_mch)
            page.get_by_role('button', name='保存渠道账号', exact=True).click()
            row = page.locator('tr').filter(has_text=channel_name)
            expect(row).to_be_visible()
            expect(row).to_contain_text('随行付（页面验收）')
            passed('新增无凭据渠道账号，只保存本地配置')
            row.get_by_role('link', name='编辑', exact=True).click()
            expect(page.get_by_label('APIv3 密钥', exact=False)).to_have_value('')
            page.get_by_label('渠道账号名称', exact=False).fill(channel_name + '-已编辑')
            page.get_by_role('button', name='保存渠道账号', exact=True).click()
            channel_name += '-已编辑'
            passed('编辑渠道账号且密钥不回显')
            page.screenshot(path=str(out / '渠道账号.png'), full_page=True)

            page.get_by_role('link', name='商户列表', exact=True).click()
            page.get_by_role('link', name='新增商户', exact=False).click()
            expect(page.locator('[data-direct-credentials]')).to_be_visible()
            expect(page.locator('[data-channel-fields]')).to_be_hidden()
            expect(page.get_by_label('渠道账号', exact=False)).to_be_hidden()
            expect(page.get_by_label('支付机构商户编号', exact=False)).to_be_hidden()
            page.get_by_text('已有服务器文件路径（可选）', exact=True).click()
            page.get_by_label('商户私钥路径', exact=False).fill('/tmp/ui-unused-private.pem')
            page.get_by_label('接入方式', exact=False).select_option('channel')
            expect(page.locator('[data-direct-credentials]')).to_be_hidden()
            expect(page.locator('[data-channel-fields]')).to_be_visible()
            expect(page.get_by_label('微信子商户号', exact=False)).to_be_visible()
            expect(page.get_by_label('渠道账号', exact=False)).to_be_visible()
            expect(page.get_by_label('支付机构商户编号', exact=False)).to_be_visible()
            page.get_by_label('商户名称', exact=False).fill(merchant_name)
            page.get_by_label('微信子商户号', exact=False).fill(merchant_mch)
            page.get_by_label('启用同步', exact=False).uncheck()
            page.get_by_role('button', name='保存商户', exact=True).click()
            expect(page).to_have_url(base + '/merchants/new/')
            passed('接入方式切换和渠道必选校验')
            page.get_by_label('渠道账号', exact=False).select_option(label=f'{channel_name} · {channel_mch}')
            page.get_by_label('支付机构商户编号', exact=False).fill('399-UI-' + token)
            page.set_viewport_size({'width': 390, 'height': 844})
            width = page.evaluate('({scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth})')
            assert width['scroll'] <= width['client'], width
            page.screenshot(path=str(out / '手机新增渠道商户.png'), full_page=True)
            passed('390px渠道商户表单无横向溢出')
            page.set_viewport_size({'width': 1440, 'height': 1080})
            page.screenshot(path=str(out / '新增渠道商户.png'), full_page=True)
            page.get_by_role('button', name='保存商户', exact=True).click()
            row = page.locator('tr').filter(has_text=merchant_name)
            expect(row).to_contain_text('渠道代调用')
            expect(row).to_contain_text(channel_name)
            expect(row).to_contain_text('399-UI-' + token)
            expect(row).to_contain_text('已停用')
            passed('新增渠道子商户并显示接入方式、所属渠道和机构商户编号')
            page.screenshot(path=str(out / '商户列表.png'), full_page=True)
            row.get_by_role('link', name='编辑', exact=True).click()
            expect(page.get_by_label('接入方式', exact=False)).to_have_value('channel')
            expect(page.get_by_label('渠道账号', exact=False).locator('option:checked')).to_have_text(f'{channel_name} · {channel_mch}')
            page.get_by_label('接入方式', exact=False).select_option('direct')
            expect(page.get_by_label('渠道账号', exact=False)).to_be_hidden()
            expect(page.get_by_label('支付机构商户编号', exact=False)).to_be_hidden()
            expect(page.get_by_label('微信商户号', exact=False)).to_be_visible()
            expect(page.get_by_label('商户私钥路径', exact=False)).to_have_value('')
            page.get_by_label('接入方式', exact=False).select_option('channel')
            expect(page.get_by_label('支付机构商户编号', exact=False)).to_have_value('399-UI-' + token)
            page.get_by_label('商户名称', exact=False).fill(merchant_name + '-已编辑')
            page.get_by_role('button', name='保存商户', exact=True).click()
            merchant_name += '-已编辑'
            expect(page.locator('tr').filter(has_text=merchant_name)).to_be_visible()
            passed('编辑渠道子商户保留关联且不保存隐藏的独立凭据')
            page.get_by_role('textbox', name='查找商户', exact=True).fill('399-UI-' + token)
            page.get_by_role('button', name='查询', exact=True).click()
            expect(page.locator('tbody tr')).to_have_count(1)
            expect(page.locator('tbody tr')).to_contain_text(merchant_name)
            passed('按支付机构商户编号查询')

            page.get_by_role('link', name='渠道账号', exact=True).click()
            row = page.locator('tr').filter(has_text=channel_name)
            row.get_by_role('link', name='删除', exact=True).click()
            page.get_by_role('button', name='确认删除渠道账号', exact=True).click()
            expect(page.locator('.messages')).to_contain_text('无法删除')
            passed('有关联商户时保护渠道账号')
            page.locator('tr').filter(has_text=channel_name).get_by_role('link', name='通知配置', exact=True).click()
            expect(page.locator('.remote-result')).to_have_count(0)
            expect(page.locator('.config-help').first).to_contain_text('先查询微信当前配置')
            page.get_by_role('button', name='执行配置操作', exact=True).click()
            expect(page.locator('.errorlist')).to_contain_text('缺少')
            passed('通知配置页面不自动查询，缺凭据时本地提示')

            page.goto(base + '/merchants/')
            page.locator('tr').filter(has_text=merchant_name).get_by_role('link', name='删除', exact=True).click()
            page.get_by_role('button', name='确认删除商户', exact=True).click()
            expect(page.locator('tr').filter(has_text=merchant_name)).to_have_count(0)
            page.get_by_role('link', name='渠道账号', exact=True).click()
            page.locator('tr').filter(has_text=channel_name).get_by_role('link', name='删除', exact=True).click()
            page.get_by_role('link', name='取消', exact=True).click()
            expect(page).to_have_url(base + '/channels/')
            page.locator('tr').filter(has_text=channel_name).get_by_role('link', name='删除', exact=True).click()
            page.get_by_role('button', name='确认删除渠道账号', exact=True).click()
            expect(page.locator('tr').filter(has_text=channel_name)).to_have_count(0)
            passed('删除临时商户、取消和删除临时渠道账号')
            assert not errors, errors
            passed('浏览器无JavaScript错误')
        except Exception:
            page.screenshot(path=str(out / '失败现场.png'), full_page=True)
            raise
        finally:
            browser.close()
            (out / 'ui-results.json').write_text(json.dumps({'checks': checks, 'javascript_errors': errors,
                'browser_closed': True, 'temporary_channel': channel_mch, 'temporary_merchant': merchant_mch,
                'notes': '本地临时数据，未配置真实凭据、未调用真实微信。'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
