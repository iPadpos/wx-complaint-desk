import io
import uuid
from datetime import date
from django.utils import timezone
from PIL import Image
from .models import Complaint
from .wechat import valid_image


class DemoGateway:
    """显式开发演示；仅在 DEBUG + ALLOW_DEMO 环境中启用，不连接微信。"""
    request_id = 'DEMO-LOCAL'

    def __init__(self, merchant): self.merchant = merchant

    def list_complaints(self, begin, end, offset=0):
        qs = Complaint.objects.filter(merchant=self.merchant, complaint_time__date__gte=date.fromisoformat(begin),
                                       complaint_time__date__lte=date.fromisoformat(end))
        return {'total_count': qs.count(), 'data': [{'complaint_id': c.complaint_id} for c in qs[offset:offset + 50]]}

    def complaint(self, cid): return dict(Complaint.objects.get(merchant=self.merchant, complaint_id=cid).detail)

    def negotiations(self, cid, offset=0):
        history = Complaint.objects.get(merchant=self.merchant, complaint_id=cid).history
        return {'total_count': len(history), 'data': history[offset:offset + 300]}

    def action(self, cid, kind, payload):
        complaint = Complaint.objects.get(merchant=self.merchant, complaint_id=cid)
        detail, history = dict(complaint.detail), list(complaint.history)
        record = {'operate_time': timezone.now().isoformat()}
        if kind in ('reply', 'immediate'):
            detail['complaint_state'], detail['incoming_user_response'] = 'PROCESSING', False
            record.update(operate_type='MERCHANT_RESPONSE', operate_details=payload.get('response_content', '即时服务消息'))
            if kind == 'immediate': record['normal_message'] = payload['message']
            if payload.get('response_images'):
                record['complaint_media_list'] = [{'media_type': 'OPERATION_IMAGE', 'media_url': [
                    'https://api.mch.weixin.qq.com/v3/merchant-service/images/' + x for x in payload['response_images']]}]
        elif kind == 'complete':
            detail['complaint_state'] = 'PROCESSED'
            record.update(operate_type='MERCHANT_CONFIRM_COMPLETE', operate_details='商户反馈处理完成')
        elif kind == 'refund':
            reject = payload['action'] == 'REJECT'
            if reject: detail['complaint_state'] = 'PROCESSED'
            record.update(operate_type='MERCHANT_REFUSE_RERUND' if reject else 'MERCHANT_APPROVE_REFUND',
                          operate_details=payload.get('reject_reason') if reject else '已同意审批，等待商户真实退款')
        history.append(record)
        complaint.detail, complaint.history = detail, history
        complaint.save(update_fields=['detail', 'history'])
        return {}

    def notification_config(self, method='GET', url=''):
        if method in ('POST', 'PUT', 'DELETE'):
            self.merchant.notify_url = url if method != 'DELETE' else ''
            self.merchant.save(update_fields=['notify_url'])
        return {'url': self.merchant.notify_url}

    def upload(self, filename, raw):
        valid_image(raw)
        return 'DEMO-' + uuid.uuid4().hex

    def download(self, media_id):
        buf = io.BytesIO()
        Image.new('RGB', (640, 400), '#edf5f0').save(buf, format='PNG')
        return buf.getvalue(), 'image/png'
