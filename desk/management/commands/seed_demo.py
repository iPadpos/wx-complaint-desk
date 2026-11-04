from datetime import timedelta
from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from desk.models import Complaint, Merchant


class Command(BaseCommand):
    help = '在显式开发演示环境创建示例商户和投诉，不接入真实微信'

    def handle(self, *args, **options):
        if not settings.ALLOW_DEMO: raise CommandError('仅 DEBUG=true 且 ALLOW_DEMO=true 时可创建示例')
        now = timezone.now()
        names = [('拾光生活', '1900000001'), ('云栖零售', '1900000002'), ('知味餐饮', '1900000003')]
        examples = [
            ('会员权益未到账', '已购买月度会员，但是账号仍显示未开通，麻烦帮我核实一下。', 'SERVICE_NOT_WORK', 'PENDING', True),
            ('申请退还重复支付款', '订单扣了两次款，其中一笔没有生成订单，希望退还重复支付的金额。', 'REFUND', 'PENDING', True),
            ('服务体验问题', '咨询的问题还没有得到解决，希望客服可以继续跟进。', 'OTHERS', 'PROCESSING', True),
            ('付款成功未收到商品', '微信支付显示成功，但没有收到商品，希望尽快处理。', 'OTHERS', 'PROCESSING', False),
            ('退款进度咨询', '已确认退款方案，请帮我核对预计到账时间。', 'REFUND', 'PROCESSING', False),
            ('订单处理已完成', '问题已经解决，谢谢你们的跟进。', 'OTHERS', 'PROCESSED', False),
        ]
        for index, (name, mch) in enumerate(names):
            merchant, created = Merchant.objects.get_or_create(mch_id=mch, defaults={'name': name, 'is_demo': True})
            if not merchant.is_demo: raise CommandError('示例商户号已被真实商户使用，拒绝覆盖')
            for j, (title, text, problem, state, incoming) in enumerate(examples[:6 if index == 0 else 3]):
                cid = f'DEMO2026100500{index}{j:03d}'
                moment = now - timedelta(hours=j * 3, minutes=12 + index * 5)
                order_no = f'SG20261005{index}{j:04d}'
                detail = {'complaint_id': cid, 'complainted_mchid': mch, 'complaint_time': moment.isoformat(),
                    'complaint_state': state, 'complaint_detail': text, 'problem_description': title, 'problem_type': problem,
                    'incoming_user_response': incoming, 'payer_phone': '138****0000', 'payer_openid': 'demo_openid',
                    'complaint_order_info': [{'out_trade_no': order_no, 'transaction_id': f'DEMO420000{index}{j:08d}', 'amount': 2990}],
                    'user_complaint_times': 2 if j == 2 else 1, 'complaint_full_refunded': state == 'PROCESSED',
                    'need_immediate_service': j == 0, 'user_tag_list': ['HIGH_RISK'] if j == 1 else [],
                    'apply_refund_amount': 2990 if problem == 'REFUND' else None}
                history = [{'operate_time': moment.isoformat(), 'operate_type': 'USER_CREATE_COMPLAINT', 'operate_details': text}]
                if state != 'PENDING': history.append({'operate_time': (moment + timedelta(minutes=6)).isoformat(),
                    'operate_type': 'MERCHANT_RESPONSE', 'operate_details': '您好，已收到您的反馈，我们正在核实订单并跟进处理。'})
                if incoming and state == 'PROCESSING': history.append({'operate_time': (moment + timedelta(minutes=18)).isoformat(),
                    'operate_type': 'USER_RESPONSE', 'operate_details': '请问目前处理到哪一步了？'})
                Complaint.objects.get_or_create(merchant=merchant, complaint_id=cid, defaults={
                    'state': state, 'problem_type': problem, 'order_no': order_no, 'complaint_time': moment,
                    'incoming_user_response': incoming, 'need_immediate_service': j == 0, 'detail': detail, 'history': history})
        group, _ = Group.objects.get_or_create(name='客诉运营')
        group.permissions.set(Permission.objects.filter(content_type__app_label='desk', codename__in=[
            'view_complaint', 'view_merchant', 'view_operation', 'reply_complaint', 'sync_complaint']))
        self.stdout.write('示例商户、投诉及运营权限组已准备，未覆盖现有投诉')
