"""仅对显式演示商户执行页面回归；使用独立无头Chromium，关闭后输出截图和JSON结果。"""
import argparse
import io
import json
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('--url', default='http://127.0.0.1:8316')
parser.add_argument('--credentials', required=True, help='仅含本地演示 admin_password 的受限JSON文件')
parser.add_argument('--output', required=True)
args = parser.parse_args()
base = args.url.rstrip('/')
out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
password = json.loads(Path(args.credentials).read_text())['admin_password']
checks, errors = [], []


def passed(name):
    checks.append({'name': name, 'result': 'passed'})
    print('PASS', name, flush=True)


with sync_playwright() as p:
    executable = os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
    browser = p.chromium.launch(headless=True, **({'executable_path': executable} if executable else {}))
    try:
        context = browser.new_context(viewport={'width': 1440, 'height': 1080}, device_scale_factor=1)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(base)
        page.get_by_label('账号', exact=False).fill('admin')
        page.get_by_label('密码', exact=False).fill(password)
        page.get_by_role('button', name='登录', exact=True).click()
        page.wait_for_url(base + '/')
        expect(page.locator('.page-heading .badge.amber')).to_be_visible()
        passed('Django原生登录与演示环境标识')
        page.screenshot(path=str(out / '工作台.png'), full_page=True)

        page.get_by_role('link', name='商户号管理', exact=True).click()
        page.get_by_role('link', name='新增商户', exact=False).click()
        page.get_by_label('商户名称', exact=False).fill('回归测试商户')
        page.get_by_label('微信商户号', exact=False).fill('1900000999')
        page.get_by_role('button', name='保存商户', exact=True).click()
        row = page.locator('tr').filter(has_text='回归测试商户')
        expect(row).to_be_visible()
        row.get_by_role('link', name='编辑', exact=True).click()
        page.get_by_label('商户名称', exact=False).fill('回归商户已修改')
        page.get_by_label('启用同步', exact=False).uncheck()
        page.get_by_role('button', name='保存商户', exact=True).click()
        page.get_by_role('textbox', name='查找商户').fill('1900000999')
        page.get_by_role('button', name='查询', exact=True).click()
        row = page.locator('tr').filter(has_text='回归商户已修改')
        expect(row).to_contain_text('已停用')
        page.screenshot(path=str(out / '商户管理.png'), full_page=True)
        row.get_by_role('link', name='删除', exact=True).click()
        page.get_by_role('button', name='确认删除商户', exact=True).click()
        expect(page.locator('tr').filter(has_text='回归商户已修改')).to_have_count(0)
        passed('商户新增、修改、查询、删除')

        page.locator('.merchant-item').filter(has_text='云栖零售').click()
        expect(page.locator('.page-heading h1')).to_contain_text('云栖零售')
        expect(page.locator('.complaint-row')).to_have_count(3)
        page.get_by_role('searchbox', name='搜索投诉或订单').fill('SG2026100510001')
        page.get_by_role('button', name='筛选', exact=True).click()
        expect(page.locator('.complaint-row')).to_have_count(1)
        expect(page.locator('.complaint-row')).to_contain_text('申请退还重复支付款')
        page.get_by_role('link', name='重置', exact=True).click()
        page.get_by_role('combobox', name='投诉状态').select_option('PENDING')
        page.get_by_role('button', name='筛选', exact=True).click()
        expect(page.locator('.complaint-row')).to_have_count(2)
        page.get_by_role('link', name='重置', exact=True).click()
        passed('切换商户与订单、状态筛选')

        page.get_by_role('button', name='同步微信投诉', exact=True).click()
        page.get_by_role('button', name='提交同步任务', exact=True).click()
        expect(page.locator('.messages')).to_contain_text('同步任务已提交')
        subprocess.run([sys.executable, 'manage.py', 'complaint_worker', '--once'], check=True, stdout=subprocess.PIPE)
        page.reload()
        page.locator('.jobs summary').click()
        expect(page.locator('.jobs')).to_contain_text('已完成')
        passed('持久同步任务由工作进程执行并显示结果')

        page.locator('.merchant-item').filter(has_text='拾光生活').click()
        list_url = page.url
        page.evaluate('window.listDocumentMarker = "keep-this-list"')
        page.locator('.complaint-row').filter(has_text='会员权益未到账').click()
        list_scroll = page.locator('.shell > .main').evaluate('(el) => el.scrollTop')
        chat = page.frame_locator('#complaint-dialog iframe')
        reply = chat.locator('.reply-section form')
        buf = io.BytesIO(); Image.new('RGB', (100, 60), '#147d58').save(buf, format='PNG')
        reply.locator('input[type=file]').set_input_files({'name': '凭证.png', 'mimeType': 'image/png', 'buffer': buf.getvalue()})
        expect(reply.locator('.upload-results')).to_contain_text('图片 1')
        reply.locator('textarea[name=content]').fill('页面回归：已核实会员订单，请查收权益恢复结果。')
        reply.get_by_role('button', name='发送回复', exact=False).click()
        expect(chat.locator('.timeline')).to_contain_text('页面回归：已核实会员订单')
        expect(chat.locator('.detail-heading .badge')).to_have_text('处理中')
        expect(chat.locator('.timeline img').first).to_be_visible()
        passed('文字与图片回复后回查协商记录和状态')

        chat.get_by_role('link', name='即时服务回复', exact=True).click()
        chat.get_by_label('人工文本回复', exact=False).fill('即时服务回归：已安排人工跟进。')
        chat.get_by_role('button', name='确认提交', exact=True).click()
        expect(chat.locator('.timeline')).to_contain_text('即时服务回归：已安排人工跟进。')
        passed('即时服务NormalMessage回复')
        chat.get_by_role('link', name='反馈完成', exact=True).click()
        chat.get_by_role('button', name='确认反馈处理完成', exact=True).click()
        expect(chat.locator('.detail-heading .badge')).to_have_text('已完成')
        passed('非退款投诉反馈完成并回查PROCESSED')

        page.get_by_role('button', name='关闭投诉详情').click()
        expect(page.locator('.complaint-row').filter(has_text='会员权益未到账').locator('[data-complaint-state]')).to_have_text('已完成')
        assert page.url == list_url and page.evaluate('window.listDocumentMarker') == 'keep-this-list'
        assert page.locator('.shell > .main').evaluate('(el) => el.scrollTop') == list_scroll
        passed('弹窗回复、即时服务和反馈完成保留外层列表并更新状态')
        page.locator('.complaint-row').filter(has_text='申请退还重复支付款').click()
        chat.get_by_role('link', name='退款审批', exact=True).click()
        page.set_viewport_size({'width': 390, 'height': 844})
        checkbox = chat.locator('input[name=acknowledged]').bounding_box()
        label = chat.locator('label[for=id_acknowledged]').bounding_box()
        assert label['x'] > checkbox['x'] and checkbox['y'] < label['y'] + 24, (checkbox, label)
        width = page.evaluate('({scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth})')
        assert width['scroll'] <= width['client'], width
        width = chat.locator('html').evaluate('(el) => ({scroll:el.scrollWidth,client:el.clientWidth})')
        assert width['scroll'] <= width['client'], width
        page.screenshot(path=str(out / '手机退款审批.png'), full_page=True)
        passed('390px退款确认项对齐且无水平溢出')
        page.set_viewport_size({'width': 1440, 'height': 1080})
        chat.get_by_label('审批结果', exact=False).select_option('APPROVE')
        chat.get_by_label('预计退款发起天数（工作日）', exact=False).fill('0')
        chat.get_by_label('我已核实审批依据', exact=False).check()
        chat.get_by_role('button', name='确认提交', exact=True).click()
        expect(chat.locator('.timeline')).to_contain_text('等待商户真实退款')
        expect(chat.locator('.detail-heading .badge')).to_have_text('待处理')
        passed('同意退款审批不标记资金已退款或投诉已完结')

        page.get_by_role('button', name='关闭投诉详情').click()
        assert page.url == list_url and page.evaluate('window.listDocumentMarker') == 'keep-this-list'
        passed('退款审批返回和关闭保留外层列表')
        page.get_by_role('link', name='通知配置', exact=True).click()
        page.set_viewport_size({'width': 390, 'height': 844})
        checkbox = page.locator('input[name=confirmed]').bounding_box()
        label = page.locator('label[for=id_confirmed]').bounding_box()
        assert label['x'] > checkbox['x'] and checkbox['y'] < label['y'] + 24, (checkbox, label)
        width = page.evaluate('({scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth})')
        assert width['scroll'] <= width['client'], width
        page.screenshot(path=str(out / '手机通知配置.png'), full_page=True)
        passed('390px通知确认项对齐且无水平溢出')
        page.set_viewport_size({'width': 1440, 'height': 1080})
        page.get_by_label('动作', exact=False).select_option('POST')
        page.get_by_role('textbox', name='通知地址').fill('https://demo.example.com/callbacks/1900000001/')
        page.get_by_label('我已核对通知地址', exact=False).check()
        page.get_by_role('button', name='执行配置操作', exact=True).click()
        expect(page.locator('.remote-result')).to_contain_text('https://demo.example.com/callbacks/1900000001/')
        page.get_by_label('动作', exact=False).select_option('PUT')
        page.get_by_role('textbox', name='通知地址').fill('https://new-demo.example.com/callbacks/1900000001/')
        page.get_by_label('我已核对通知地址', exact=False).check()
        page.get_by_role('button', name='执行配置操作', exact=True).click()
        expect(page.locator('.remote-result')).to_contain_text('https://new-demo.example.com')
        page.get_by_label('动作', exact=False).select_option('GET')
        page.get_by_role('button', name='执行配置操作', exact=True).click()
        expect(page.locator('.remote-result')).to_contain_text('https://new-demo.example.com')
        page.get_by_label('动作', exact=False).select_option('DELETE')
        page.get_by_label('我已核对通知地址', exact=False).check()
        page.get_by_role('button', name='执行配置操作', exact=True).click()
        expect(page.locator('.remote-result')).to_contain_text('未配置通知地址')
        passed('演示通知配置创建、更新、查询、删除及回查')

        page.get_by_role('link', name='操作记录', exact=True).click()
        expect(page.locator('table')).to_contain_text('已成功')
        page.screenshot(path=str(out / '操作记录.png'), full_page=True)
        page.get_by_role('link', name='投诉处理', exact=True).click()
        page.set_viewport_size({'width': 390, 'height': 844})
        page.screenshot(path=str(out / '手机工作台.png'), full_page=True)
        width = page.evaluate('({scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth})')
        assert width['scroll'] <= width['client'], width
        passed('390px手机布局无水平溢出')
        assert not errors, errors
        passed('浏览器无JavaScript错误')
        page.get_by_role('button', name='退出', exact=True).click()
        expect(page.get_by_role('button', name='登录', exact=True)).to_be_visible()
        passed('退出后恢复登录页')
    finally:
        browser.close()
        (out / 'ui-results.json').write_text(json.dumps({'checks': checks, 'javascript_errors': errors,
            'browser_closed': True, 'notes': '独立Playwright Chromium；只对演示商户操作，未连接真实微信。'}, ensure_ascii=False, indent=2))
