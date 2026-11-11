import hashlib
import json
import uuid
from datetime import timedelta
from urllib.parse import unquote, urlsplit

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import ChannelNotification, Complaint, Job, Media, Merchant, Notification, Operation, ViolationRecord, WechatChannel
from .wechat import WechatError, gateway_for


class _ComplaintDataError(WechatError):
    pass


def enqueue(merchant, kind, payload, key=None):
    if key:
        job, _ = Job.objects.get_or_create(dedupe_key=f'{merchant.pk}:{key}', defaults={
            'merchant': merchant, 'kind': kind, 'payload': payload})
        return job
    return Job.objects.create(merchant=merchant, kind=kind, payload=payload)


def _channel_scope_row(merchant, cid, detail, gateway):
    """只有按目标子商户筛选的列表，才能证明返回渠道号的投诉归属。"""
    stamp = parse_datetime(detail.get('complaint_time', ''))
    if stamp is None:
        raise _ComplaintDataError('微信投诉时间格式错误')
    if timezone.is_naive(stamp):
        stamp = timezone.make_aware(stamp)
    day = timezone.localtime(stamp).date().isoformat()
    scoped_gateway = gateway if getattr(gateway, 'merchant', None) == merchant else gateway_for(merchant)
    offset = 0
    while True:
        page = scoped_gateway.list_complaints(day, day, offset)
        rows = page.get('data', [])
        if not isinstance(rows, list):
            raise WechatError('投诉列表格式错误')
        for row in rows:
            if not isinstance(row, dict):
                raise WechatError('投诉列表格式错误')
            if row.get('complaint_id') == cid:
                _check_channel_scope_row(merchant, cid, row)
                return row
        offset += len(rows)
        if not rows or offset >= page.get('total_count', offset if len(rows) < 50 else offset + 1):
            return None


def _check_channel_scope_row(merchant, cid, row):
    if (not isinstance(row, dict) or row.get('complaint_id') != cid or
            row.get('complainted_mchid') not in (merchant.mch_id, merchant.channel.mch_id)):
        raise _ComplaintDataError('渠道投诉查询结果与目标子商户不匹配')


def _validate_complaint_owner(merchant, cid, detail, gateway, scoped_row=None,
                              error_message='微信返回的投诉单或商户归属不匹配'):
    default_mchid = None if merchant.access_mode == 'channel' else merchant.mch_id
    owner = detail.get('complainted_mchid', default_mchid)
    if detail.get('complaint_id') != cid:
        raise _ComplaintDataError(error_message)
    if owner == merchant.mch_id:
        return None
    if merchant.access_mode != 'channel' or not merchant.channel_id or owner != merchant.channel.mch_id:
        raise _ComplaintDataError(error_message)
    if scoped_row is None:
        scoped_row = _channel_scope_row(merchant, cid, detail, gateway)
    elif getattr(gateway, 'merchant', None) != merchant:
        raise _ComplaintDataError('渠道投诉查询上下文与目标子商户不匹配')
    _check_channel_scope_row(merchant, cid, scoped_row)
    if Complaint.objects.filter(merchant__access_mode='channel', merchant__channel_id=merchant.channel_id,
                                complaint_id=cid).exclude(merchant=merchant).exists():
        raise _ComplaintDataError('该渠道投诉已关联其他子商户，请核对归属')
    return scoped_row


def sync_one(merchant, cid, gateway=None, *, detail=None, scoped_row=None):
    gw = gateway or gateway_for(merchant)
    if detail is None: detail = gw.complaint(cid)
    _validate_complaint_owner(merchant, cid, detail, gw, scoped_row)
    history, offset = [], 0
    while True:
        page = gw.negotiations(cid, offset)
        rows = page.get('data', [])
        if not isinstance(rows, list): raise _ComplaintDataError('协商历史格式错误')
        history.extend(rows)
        offset += len(rows)
        if not rows or offset >= page.get('total_count', offset if len(rows) < 300 else offset + 1): break
    try:
        complaint_time = parse_datetime(detail.get('complaint_time', ''))
    except (TypeError, ValueError) as error:
        raise _ComplaintDataError('微信投诉时间格式错误') from error
    if complaint_time is None: raise _ComplaintDataError('微信投诉时间格式错误')
    if timezone.is_naive(complaint_time): complaint_time = timezone.make_aware(complaint_time)
    orders = detail.get('complaint_order_info', [])
    order_no = orders[0].get('out_trade_no', '') if orders else ''
    complaint, _ = Complaint.objects.update_or_create(merchant=merchant, complaint_id=cid, defaults={
        'state': detail.get('complaint_state', 'PENDING'), 'complaint_time': complaint_time,
        'order_no': order_no, 'problem_type': detail.get('problem_type', ''),
        'incoming_user_response': detail.get('incoming_user_response', False),
        'need_immediate_service': detail.get('need_immediate_service', False),
        'in_platform_service': detail.get('in_platform_service', False), 'detail': detail, 'history': history})
    return complaint


def sync_range(merchant, begin, end):
    from datetime import date
    begin, end = date.fromisoformat(begin), date.fromisoformat(end)
    if begin > end or (end - begin).days > 365: raise WechatError('同步日期需为一年以内的有效范围')
    gw = gateway_for(merchant)
    count, failed, first_error = 0, [], ''
    while begin <= end:
        stop, offset = min(end, begin + timedelta(days=29)), 0
        while True:
            page = gw.list_complaints(begin.isoformat(), stop.isoformat(), offset)
            rows = page.get('data', [])
            if not isinstance(rows, list): raise WechatError('投诉列表格式错误')
            for row in rows:
                cid = row['complaint_id']
                try:
                    sync_one(merchant, cid, gw,
                             scoped_row=row if merchant.access_mode == 'channel' else None)
                    count += 1
                except WechatError as error:
                    if error.code != 'RESOURCE_NOT_EXISTS' and not isinstance(error, _ComplaintDataError): raise
                    if not failed: first_error = str(error)
                    failed.append(cid)
            offset += len(rows)
            if not rows or offset >= page.get('total_count', offset if len(rows) < 50 else offset + 1): break
        begin = stop + timedelta(days=1)
    if failed: raise WechatError((f'部分投诉同步失败（首个原因：{first_error}）：' + '、'.join(failed))[:240])
    merchant.last_synced_at, merchant.last_sync_error = timezone.now(), ''
    merchant.save(update_fields=['last_synced_at', 'last_sync_error'])
    return count


def receive_notification(merchant, event_id, payload):
    # 通知与持久任务在一个数据库事务内落库；失败不回204，由微信重投。
    with transaction.atomic():
        event, created = Notification.objects.get_or_create(merchant=merchant, event_id=event_id,
                                                             defaults={'payload': payload})
        if created:
            enqueue(merchant, 'detail', {'complaint_id': payload['complaint_id']}, 'event:' + event_id)
    return event


def receive_channel_notification(channel, event_id, payload):
    # 渠道回调通常只有投诉号：先持久入队并应答，归属查询由同一 worker 处理。
    with transaction.atomic():
        event, created = ChannelNotification.objects.get_or_create(channel=channel, event_id=event_id,
                                                                    defaults={'payload': payload})
        if created:
            if channel.enabled:
                Job.objects.create(channel=channel, kind='channel_detail',
                    payload={'complaint_id': payload['complaint_id']}, dedupe_key=f'channel:{channel.pk}:event:{event_id}')
            else:
                Job.objects.create(channel=channel, kind='channel_detail',
                    payload={'complaint_id': payload['complaint_id']}, state='failed',
                    error='渠道已停用，启用后可重试', finished_at=timezone.now(), dedupe_key=None)
    return event


def receive_violation_notification(channel, event_id, event_type, payload):
    # 处置记录无需回查接口，直接落库；重复通知按通知 ID 去重。渠道停用时仍保存。
    try: punish_time = parse_datetime(payload.get('punish_time') or '')
    except (TypeError, ValueError): punish_time = None
    event, _ = ViolationRecord.objects.get_or_create(channel=channel, event_id=event_id, defaults={
        'event_type': event_type, 'sub_mchid': payload['sub_mchid'], 'record_id': payload['record_id'],
        'punish_time': punish_time, 'payload': payload})
    return event


def sync_channel_notification(channel, cid):
    gw = gateway_for(channel)
    detail = gw.complaint(cid)
    if detail.get('complaint_id') != cid or not isinstance(detail.get('complainted_mchid'), str):
        raise WechatError('渠道投诉详情缺少有效商户归属')
    if detail['complainted_mchid'] == channel.mch_id:
        matches = []
        for candidate in Merchant.objects.select_related('channel').filter(access_mode='channel', channel=channel):
            scoped_gateway = gateway_for(candidate)
            row = _channel_scope_row(candidate, cid, detail, scoped_gateway)
            if row is not None:
                matches.append((candidate, scoped_gateway, row))
                if len(matches) > 1:
                    raise WechatError('渠道投诉匹配到多个子商户，归属未确认')
        if not matches:
            raise WechatError('渠道投诉暂未匹配到已接入子商户，请稍后重试')
        merchant, scoped_gateway, row = matches[0]
        sync_one(merchant, cid, scoped_gateway, detail=detail, scoped_row=row)
        return ''
    merchant = Merchant.objects.select_related('channel').filter(
        access_mode='channel', channel=channel, mch_id=detail['complainted_mchid']).first()
    if not merchant:
        # 渠道推送覆盖名下全部子商户；仅导入工作台已接入的商户。
        return '微信子商户尚未接入此渠道，未导入投诉'
    sync_one(merchant, cid, gw, detail=detail)
    return ''


def perform_operation(merchant, complaint, user, operation_id, kind, payload):
    """本地幂等键不传微信；不确定结果必须先回查，绝不自动重发写接口。"""
    try: operation_id = uuid.UUID(str(operation_id))
    except (ValueError, TypeError): raise WechatError('操作标识无效，请刷新页面')
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    is_channel = isinstance(merchant, WechatChannel)
    owner = {'channel': merchant} if is_channel else {'merchant': merchant}
    if not complaint and not is_channel and merchant.access_mode == 'channel':
        raise WechatError('渠道投诉通知请在渠道账号中统一配置')
    with transaction.atomic():
        # 同一投诉的未确认写入串行化；通知配置则按商户串行。
        if complaint:
            Complaint.objects.select_for_update().get(pk=complaint.pk)
        else:
            type(merchant).objects.select_for_update().get(pk=merchant.pk)
        existing = Operation.objects.filter(pk=operation_id).first()
        if existing:
            if (existing.merchant_id != (None if is_channel else merchant.pk)
                or existing.channel_id != (merchant.pk if is_channel else None)
                or existing.complaint_id != (complaint.pk if complaint else None)
                or existing.kind != kind or existing.payload_hash != digest or existing.user_id != user.pk):
                raise WechatError('操作标识与请求不一致')
            if existing.state == 'failed': return existing, '该操作此前未成功，请重新填写提交'
            if existing.state in ('pending', 'unknown'): return existing, '该操作结果未确认，请先回查'
            return existing, '该操作已提交，未重复发送'
        unresolved = Operation.objects.filter(**owner, complaint=complaint, state__in=['pending', 'unknown'])
        if unresolved.exists(): raise WechatError('此投诉或配置有结果未确认的操作，请先同步回查并在操作记录中确认')
        operation = Operation.objects.create(id=operation_id, **owner, complaint=complaint,
                                              user=user, kind=kind, payload_hash=digest)
    try:
        gw = gateway_for(merchant)
        verified_scope = None
        if complaint:
            # 使用最新远端状态验证可执行条件，防止本地状态滞后。
            latest = gw.complaint(complaint.complaint_id)
            verified_scope = _validate_complaint_owner(merchant, complaint.complaint_id, latest, gw,
                error_message='投诉单或商户归属不匹配，操作已停止')
            state = latest.get('complaint_state')
            if kind == 'complete' and (state != 'PROCESSING' or latest.get('problem_type') == 'REFUND'):
                raise WechatError('只有已回复的非退款投诉可反馈完成；退款投诉请使用退款审批')
            if kind in ('reply', 'immediate', 'refund') and state == 'PROCESSED':
                raise WechatError('该投诉已完成，请同步查看最新状态')
            if kind == 'refund' and latest.get('problem_type') != 'REFUND': raise WechatError('此投诉不是退款申请')
            if kind == 'immediate' and not latest.get('need_immediate_service'): raise WechatError('此单无需即时服务')
            # 渠道返回渠道号时，以已核验的微信原始被诉号调用写接口；本地归属仍是子商户。
            outbound = ({**payload, 'complainted_mchid': latest['complainted_mchid']}
                        if verified_scope is not None and 'complainted_mchid' in payload else payload)
            gw.action(complaint.complaint_id, kind, outbound)
        elif kind.startswith('violation:'):
            gw.violation_notification_config(kind.split(':', 1)[1], payload.get('url', ''))
        else:
            gw.notification_config(kind.split(':', 1)[1], payload.get('url', ''))
        operation.state, operation.result, operation.request_id = 'success', '微信已接受操作', gw.request_id
        operation.save(update_fields=['state', 'result', 'request_id'])
    except WechatError as error:
        operation.state = 'unknown' if error.unknown else 'failed'
        operation.result, operation.request_id = str(error)[:240], error.request_id
        operation.save(update_fields=['state', 'result', 'request_id'])
        return operation, operation.result
    except Exception:
        operation.state, operation.result = 'unknown', '操作结果未知，请回查微信状态后确认'
        operation.save(update_fields=['state', 'result'])
        return operation, operation.result
    if complaint:
        try:
            sync_one(merchant, complaint.complaint_id, gw, scoped_row=verified_scope)
        except Exception:
            enqueue(merchant, 'detail', {'complaint_id': complaint.complaint_id})
            return operation, '微信已接受操作，回查暂未完成，已加入同步任务'
    return operation, operation.result


def media_ids(data):
    """只提取微信指定媒体源的ID，避免以返回URL执行任意外部请求。"""
    result = set()
    if isinstance(data, dict):
        for key, value in data.items():
            if key in ('media_url', 'image_list'):
                for url in value if isinstance(value, list) else []:
                    parsed = urlsplit(url)
                    prefix = '/v3/merchant-service/images/'
                    if parsed.scheme == 'https' and parsed.hostname in ('api.mch.weixin.qq.com', 'api2.mch.weixin.qq.com') and parsed.path.startswith(prefix):
                        result.add(unquote(parsed.path[len(prefix):]))
            elif key == 'media_id' and isinstance(value, str): result.add(value)
            else: result.update(media_ids(value))
    elif isinstance(data, list):
        for value in data: result.update(media_ids(value))
    return result


def allowed_media(complaint):
    return media_ids(complaint.detail) | media_ids(complaint.history) | set(
        Media.objects.filter(complaint=complaint, merchant=complaint.merchant).values_list('media_id', flat=True))


def process_job():
    now = timezone.now()
    with transaction.atomic():
        # 仅重投只读同步任务。写接口从不进此队列。
        Job.objects.filter(state='running', locked_at__lt=now - timedelta(minutes=15)).update(state='queued')
        job = Job.objects.select_for_update(skip_locked=True).filter(state='queued', available_at__lte=now).order_by('id').first()
        if not job: return False
        job.state, job.locked_at, job.attempts = 'running', now, job.attempts + 1
        job.save(update_fields=['state', 'locked_at', 'attempts'])
    try:
        result = ''
        if job.kind == 'channel_detail' and job.channel_id:
            result = sync_channel_notification(job.channel, job.payload['complaint_id'])
        elif job.channel_id: raise WechatError('未知渠道任务类型')
        elif job.kind == 'sync': sync_range(job.merchant, job.payload['begin'], job.payload['end'])
        elif job.kind == 'detail': sync_one(job.merchant, job.payload['complaint_id'])
        else: raise WechatError('未知任务类型')
        job.state, job.error, job.finished_at, job.dedupe_key = 'done', result, timezone.now(), None
    except Exception as error:
        job.error = str(error)[:240] if isinstance(error, WechatError) else '同步异常，请核对配置及运行日志'
        if job.merchant_id:
            job.merchant.last_sync_error = job.error
            job.merchant.save(update_fields=['last_sync_error'])
        job.state = 'failed' if job.attempts >= 5 else 'queued'
        job.available_at = timezone.now() + timedelta(seconds=min(600, 15 * 2 ** job.attempts))
        if job.state == 'failed': job.dedupe_key = None
    job.save()
    return True
