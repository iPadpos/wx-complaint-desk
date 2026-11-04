import json
from django import template
from django.utils import timezone
from django.utils.dateparse import parse_datetime

register = template.Library()


@register.filter
def money(value):
    try: return f'{int(value) / 100:.2f}'
    except (ValueError, TypeError): return '—'


@register.filter
def wechat_time(value):
    try:
        parsed = parse_datetime(value)
        return timezone.localtime(parsed).strftime('%m-%d %H:%M') if parsed else value
    except (ValueError, TypeError): return value


@register.filter
def pretty_json(value): return json.dumps(value, ensure_ascii=False, indent=2)


@register.filter
def operate_type(value):
    return {'USER_CREATE_COMPLAINT': '用户提交投诉', 'USER_CONTINUE_COMPLAINT': '用户继续投诉',
            'USER_RESPONSE': '用户新留言', 'MERCHANT_RESPONSE': '商户回复', 'PLATFORM_RESPONSE': '微信客服留言',
            'MERCHANT_CONFIRM_COMPLETE': '商户反馈完成', 'MERCHANT_APPROVE_REFUND': '商户同意退款审批',
            'MERCHANT_REFUSE_RERUND': '商户拒绝退款', 'USER_APPLY_PLATFORM_SERVICE': '用户申请平台协助',
            'COMPLAINT_REFUND_RECEIVED_SYSTEM_MESSAGE': '退款到账', 'USER_SUBMIT_SATISFACTION': '用户评价'}.get(value, value)


@register.filter
def operation_kind(value):
    return {'reply': '回复用户', 'immediate': '即时服务回复', 'complete': '反馈处理完成', 'refund': '退款审批',
            'notify:POST': '创建通知配置', 'notify:PUT': '更新通知配置', 'notify:DELETE': '删除通知配置'}.get(value, value)


@register.filter
def operation_state(value):
    return {'pending': '提交中', 'success': '已成功', 'failed': '未成功', 'unknown': '结果未知'}.get(value, value)


@register.filter
def job_state(value):
    return {'queued': '等待执行', 'running': '执行中', 'done': '已完成', 'failed': '已失败'}.get(value, value)


@register.filter
def job_kind(value): return {'sync': '批量同步', 'detail': '同步详情'}.get(value, value)


@register.filter
def user_tag(value): return {'HIGH_RISK': '高风险 · 优先处理', 'TRUSTED': '极速退款用户'}.get(value, value)
