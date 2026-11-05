import uuid
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db import DatabaseError
from django.db.models import Count, Q
from django.db.models.deletion import ProtectedError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET, require_POST

from .forms import ChannelForm, ImmediateForm, MerchantForm, NotificationForm, RefundForm, ReplyForm, SyncForm
from .credentials import CredentialStorageError, save_channel_configuration, save_merchant_configuration
from .models import Complaint, Job, Media, Merchant, Operation, WechatChannel
from .services import allowed_media, enqueue, media_ids, perform_operation, receive_channel_notification, receive_notification
from .wechat import WechatError, configuration_errors, decrypt_notification, gateway_for, verify_signature


def recent_pending(prefix=''):
    # 待处理统计只算近30天，过旧投诉无处理需求。
    return Q(**{f'{prefix}state': 'PENDING', f'{prefix}complaint_time__gte': timezone.now() - timedelta(days=30)})


def context(merchant=None):
    merchants = Merchant.objects.annotate(pending=Count('complaints', filter=recent_pending('complaints__')))
    return {'merchants': merchants, 'merchant': merchant}


@login_required
@permission_required('desk.view_complaint', raise_exception=True)
def workspace(request, merchant_id=None):
    merchant = get_object_or_404(Merchant, pk=merchant_id) if merchant_id else Merchant.objects.first()
    ctx = context(merchant)
    if not merchant: return render(request, 'desk/workspace.html', ctx)
    base = Complaint.objects.filter(merchant=merchant)
    stats = {'total': base.count(), 'pending': base.filter(recent_pending()).count(),
             'processing': base.filter(state='PROCESSING').count(), 'incoming': base.filter(incoming_user_response=True).count()}
    qs = base
    query = request.GET.get('q', '').strip()[:128]
    if query: qs = qs.filter(Q(complaint_id__icontains=query) | Q(order_no__icontains=query))
    state = request.GET.get('state', '')
    if state in ('PENDING', 'PROCESSING', 'PROCESSED'): qs = qs.filter(state=state)
    if request.GET.get('incoming') == '1': qs = qs.filter(incoming_user_response=True)
    if request.GET.get('problem') in ('REFUND', 'SERVICE_NOT_WORK', 'OTHERS'): qs = qs.filter(problem_type=request.GET['problem'])
    # 筛选无效日期时给出提示，避免500。
    from django.utils.dateparse import parse_date
    for key, lookup in [('begin', 'complaint_time__date__gte'), ('end', 'complaint_time__date__lte')]:
        value = request.GET.get(key, '')
        if value:
            try: parsed = parse_date(value)
            except ValueError: parsed = None
            if parsed: qs = qs.filter(**{lookup: parsed})
            else: messages.error(request, '筛选日期无效')
    page = Paginator(qs, 15).get_page(request.GET.get('page'))
    cid = request.GET.get('complaint')
    initial_complaint = get_object_or_404(base, pk=cid) if cid and cid.isdecimal() else None
    today = timezone.localdate()
    ctx.update(stats=stats, page=page, initial_complaint=initial_complaint, errors=configuration_errors(merchant),
               sync_form=SyncForm(initial={'begin': today - timedelta(days=2), 'end': today}),
               query=query, states=Complaint.STATES, operation_id=uuid.uuid4(),
               jobs=Job.objects.filter(merchant=merchant).order_by('-id')[:5])
    params = request.GET.copy()
    for key in ['page', 'complaint']: params.pop(key, None)
    ctx['filter_query'] = params.urlencode()
    return render(request, 'desk/workspace.html', ctx)


@login_required
@permission_required('desk.view_complaint', raise_exception=True)
@require_GET
@xframe_options_sameorigin
def complaint_detail(request, merchant_id, complaint_pk):
    complaint = get_object_or_404(Complaint.objects.select_related('merchant'),
                                 pk=complaint_pk, merchant_id=merchant_id)
    history = []
    for item in complaint.history:
        row = dict(item)
        row['media_ids'] = sorted(media_ids(item))
        history.append(row)
    stats = Complaint.objects.filter(merchant_id=merchant_id).aggregate(
        total=Count('pk'), pending=Count('pk', filter=recent_pending()),
        processing=Count('pk', filter=Q(state='PROCESSING')),
        incoming=Count('pk', filter=Q(incoming_user_response=True)))
    return render(request, 'desk/complaint_detail.html', {
        **context(complaint.merchant), 'selected': complaint, 'detail': complaint.detail,
        'history': history, 'images': sorted(media_ids(complaint.detail)),
        'reply_form': ReplyForm(complaint=complaint), 'stats': stats, 'chat_page': True,
        'operations': Operation.objects.filter(complaint=complaint)[:5],
    })


@login_required
@permission_required('desk.view_merchant', raise_exception=True)
def merchant_list(request):
    ctx = context()
    rows = Merchant.objects.select_related('channel')
    query = request.GET.get('q', '').strip()
    if query: rows = rows.filter(Q(name__icontains=query) | Q(mch_id__icontains=query) | Q(provider_merchant_id__icontains=query))
    ctx.update(rows=rows, query=query, menu='merchants')
    return render(request, 'desk/merchants.html', ctx)


@login_required
@permission_required('desk.view_wechatchannel', raise_exception=True)
def channel_list(request):
    query = request.GET.get('q', '').strip()[:128]
    rows = WechatChannel.objects.annotate(merchant_count=Count('merchants'))
    if query: rows = rows.filter(Q(name__icontains=query) | Q(mch_id__icontains=query) | Q(provider__icontains=query))
    return render(request, 'desk/channels.html', {**context(), 'rows': rows, 'query': query,
                                               'menu': 'merchants', 'channel_tab': True})


@login_required
def channel_edit(request, channel_id=None):
    perm = 'desk.change_wechatchannel' if channel_id else 'desk.add_wechatchannel'
    if not request.user.has_perm(perm): return HttpResponse('无渠道账号管理权限', status=403)
    channel = get_object_or_404(WechatChannel, pk=channel_id) if channel_id else None
    form = ChannelForm(request.POST or None, request.FILES or None, instance=channel)
    if request.method == 'POST' and form.is_valid():
        try:
            saved = save_channel_configuration(form)
        except CredentialStorageError as error:
            form.add_error(None, str(error))
        else:
            key_status = 'APIv3 密钥已保存' if saved.api_v3_key else 'APIv3 密钥尚未配置'
            messages.success(request, f'渠道账号已保存；{key_status}')
            return redirect('channel_list')
    return render(request, 'desk/channel_form.html', {**context(), 'form': form, 'channel': channel,
        'menu': 'merchants', 'title': '编辑渠道账号' if channel else '新增渠道账号'})


@login_required
@permission_required('desk.delete_wechatchannel', raise_exception=True)
def channel_delete(request, channel_id):
    channel = get_object_or_404(WechatChannel, pk=channel_id)
    if request.method == 'POST':
        try:
            channel.delete()
            messages.success(request, '渠道账号已删除')
        except ProtectedError:
            messages.error(request, '该渠道已有商户、通知、任务或操作记录，无法删除。可编辑渠道并停用。')
        return redirect('channel_list')
    return render(request, 'desk/confirm.html', {**context(), 'title': '删除渠道账号',
        'description': f'删除“{channel.name}（{channel.mch_id}）”？', 'cancel_url': reverse('channel_list')})


@login_required
def merchant_edit(request, merchant_id=None):
    perm = 'desk.change_merchant' if merchant_id else 'desk.add_merchant'
    if not request.user.has_perm(perm): return HttpResponse('无商户管理权限', status=403)
    merchant = get_object_or_404(Merchant, pk=merchant_id) if merchant_id else None
    form = MerchantForm(request.POST or None, request.FILES or None, instance=merchant)
    if request.method == 'POST' and form.is_valid():
        try:
            save_merchant_configuration(form)
        except CredentialStorageError as error:
            form.add_error(None, str(error))
        else:
            messages.success(request, '商户资料已保存')
            return redirect('merchant_list')
    ctx = context(merchant)
    ctx.update(form=form, menu='merchants', title='编辑商户' if merchant else '新增商户')
    return render(request, 'desk/merchant_form.html', ctx)


@login_required
@permission_required('desk.delete_merchant', raise_exception=True)
def merchant_delete(request, merchant_id):
    merchant = get_object_or_404(Merchant, pk=merchant_id)
    if request.method == 'POST':
        try:
            merchant.delete()
            messages.success(request, '商户已删除')
        except ProtectedError:
            messages.error(request, '该商户已有投诉、任务或操作记录，无法删除。可编辑商户并关闭同步。')
        return redirect('merchant_list')
    return render(request, 'desk/confirm.html', {**context(merchant), 'title': '删除商户',
                  'description': f'删除“{merchant.name}（{merchant.mch_id}）”？'})


def back(complaint, request):
    url = reverse('complaint_detail', args=[complaint.merchant_id, complaint.pk])
    return redirect(url + ('?dialog=1' if request.GET.get('dialog') else ''))


@login_required
@permission_required('desk.sync_complaint', raise_exception=True)
@require_POST
@xframe_options_sameorigin
def sync(request, merchant_id, complaint_pk=None):
    merchant = get_object_or_404(Merchant, pk=merchant_id)
    if complaint_pk:
        complaint = get_object_or_404(Complaint, pk=complaint_pk, merchant=merchant)
        enqueue(merchant, 'detail', {'complaint_id': complaint.complaint_id}, f'detail:{complaint.pk}')
    else:
        form = SyncForm(request.POST)
        if not form.is_valid():
            messages.error(request, '；'.join(str(x) for rows in form.errors.values() for x in rows))
            return redirect('workspace', merchant_id=merchant.pk)
        data = {key: value.isoformat() for key, value in form.cleaned_data.items()}
        enqueue(merchant, 'sync', data, f'sync:{data["begin"]}:{data["end"]}')
    if complaint_pk:
        messages.success(request, '同步任务已提交')
        return back(complaint, request)
    messages.success(request, '同步任务已提交')
    return redirect('workspace', merchant_id=merchant.pk)


@login_required
@require_POST
@xframe_options_sameorigin
def reply(request, merchant_id, complaint_pk):
    if not request.user.has_perm('desk.reply_complaint'): return HttpResponse('无回复权限', status=403)
    complaint = get_object_or_404(Complaint, pk=complaint_pk, merchant_id=merchant_id)
    form = ReplyForm(request.POST, complaint=complaint)
    if form.is_valid():
        try:
            op, msg = perform_operation(complaint.merchant, complaint, request.user,
                form.cleaned_data['operation_id'], 'reply', form.payload())
            (messages.success if op.state == 'success' else messages.error)(request, msg)
            return back(complaint, request)
        except WechatError as error: form.add_error(None, str(error))
    return render(request, 'desk/action_form.html', {**context(complaint.merchant), 'form': form,
                  'complaint': complaint, 'title': '回复用户', 'is_reply': True})


@login_required
@permission_required('desk.complete_complaint', raise_exception=True)
@xframe_options_sameorigin
def complete(request, merchant_id, complaint_pk):
    complaint = get_object_or_404(Complaint, pk=complaint_pk, merchant_id=merchant_id)
    if request.method == 'POST':
        try:
            op, msg = perform_operation(complaint.merchant, complaint, request.user, request.POST.get('operation_id'),
                                      'complete', {'complainted_mchid': complaint.merchant.mch_id})
            (messages.success if op.state == 'success' else messages.error)(request, msg)
        except WechatError as error: messages.error(request, str(error))
        return back(complaint, request)
    return render(request, 'desk/confirm.html', {**context(complaint.merchant), 'title': '反馈处理完成',
        'complaint': complaint, 'operation_id': uuid.uuid4(),
        'description': '确认用户问题已解决？'})


@login_required
@xframe_options_sameorigin
def refund(request, merchant_id, complaint_pk):
    if not request.user.has_perm('desk.refund_complaint'): return HttpResponse('无退款审批权限', status=403)
    complaint = get_object_or_404(Complaint, pk=complaint_pk, merchant_id=merchant_id)
    form = RefundForm(request.POST or None, complaint=complaint)
    if request.method == 'POST' and form.is_valid():
        try:
            op, msg = perform_operation(complaint.merchant, complaint, request.user,
                                       form.cleaned_data['operation_id'], 'refund', form.payload())
            (messages.success if op.state == 'success' else messages.error)(request, msg)
            return back(complaint, request)
        except WechatError as error: form.add_error(None, str(error))
    return render(request, 'desk/action_form.html', {**context(complaint.merchant), 'form': form,
        'complaint': complaint, 'title': '退款审批反馈', 'upload_enabled': True,
        'description': '审批不执行资金退款。'})


@login_required
@permission_required('desk.reply_complaint', raise_exception=True)
@xframe_options_sameorigin
def immediate(request, merchant_id, complaint_pk):
    complaint = get_object_or_404(Complaint, pk=complaint_pk, merchant_id=merchant_id)
    form = ImmediateForm(request.POST or None, complaint=complaint)
    if request.method == 'POST' and form.is_valid():
        try:
            op, msg = perform_operation(complaint.merchant, complaint, request.user,
                form.cleaned_data['operation_id'], 'immediate', form.payload())
            (messages.success if op.state == 'success' else messages.error)(request, msg)
            return back(complaint, request)
        except WechatError as error: form.add_error(None, str(error))
    return render(request, 'desk/action_form.html', {**context(complaint.merchant), 'form': form,
        'complaint': complaint, 'title': '即时服务回复', 'immediate': True, 'upload_enabled': True})


@login_required
@permission_required('desk.configure_notification', raise_exception=True)
def notifications(request, merchant_id):
    merchant = get_object_or_404(Merchant, pk=merchant_id)
    if merchant.access_mode == 'channel':
        return redirect('channel_notifications', channel_id=merchant.channel_id)
    form = NotificationForm(request.POST or None, merchant=merchant, initial={'url': merchant.notify_url})
    remote = None
    snapshot_key = f'merchant_notification:{merchant.pk}'

    def query_remote(gw):
        try: result = gw.notification_config()
        except WechatError as error:
            if error.code != 'RESOURCE_NOT_EXISTS': raise
            result = {}
        if result.get('mchid', merchant.mch_id) != merchant.mch_id:
            raise WechatError('微信返回的通知配置账号不匹配')
        return result

    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        try:
            gw = gateway_for(merchant)
            if data['action'] == 'GET':
                remote = query_remote(gw)
                request.session[snapshot_key] = {'url': remote.get('url', '')}
            else:
                snapshot = request.session.get(snapshot_key)
                if snapshot is None: raise WechatError('请先查询微信当前通知配置，再确认是否需要修改')
                remote = query_remote(gw)
                if remote.get('url', '') != snapshot['url']:
                    request.session.pop(snapshot_key, None)
                    raise WechatError('微信通知配置已变化，请重新查询并核对现有地址用途')
                op, msg = perform_operation(merchant, None, request.user, data['operation_id'],
                    'notify:' + data['action'], {'url': data['url']} if data['action'] != 'DELETE' else {})
                (messages.success if op.state == 'success' else messages.error)(request, msg)
                request.session.pop(snapshot_key, None)
                if op.state == 'success':
                    # 接受结果后回查；未存在返回提示而不影响写入审计。
                    try: remote = query_remote(gw)
                    except WechatError: messages.info(request, '微信已接受操作，配置回查暂未成功，请再次查询')
                form = NotificationForm(merchant=merchant, initial={'url': data['url']})
        except WechatError as error: form.add_error(None, str(error))
    return render(request, 'desk/notifications.html', {**context(merchant), 'form': form, 'remote': remote})


@login_required
@permission_required('desk.view_wechatchannel', raise_exception=True)
@permission_required('desk.configure_notification', raise_exception=True)
def channel_notifications(request, channel_id):
    channel = get_object_or_404(WechatChannel, pk=channel_id)
    form = NotificationForm(request.POST or None, merchant=channel, initial={'url': channel.notify_url})
    remote = None
    snapshot_key = f'channel_notification:{channel.pk}'

    def query_remote(gw):
        try: result = gw.notification_config()
        except WechatError as error:
            if error.code != 'RESOURCE_NOT_EXISTS': raise
            result = {}
        if result.get('mchid', channel.mch_id) != channel.mch_id:
            raise WechatError('微信返回的通知配置账号不匹配')
        return result

    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        try:
            gw = gateway_for(channel)
            if data['action'] == 'GET':
                remote = query_remote(gw)
                request.session[snapshot_key] = {'url': remote.get('url', '')}
            else:
                snapshot = request.session.get(snapshot_key)
                if snapshot is None: raise WechatError('请先查询微信当前通知配置，再确认是否需要修改')
                remote = query_remote(gw)
                if remote.get('url', '') != snapshot['url']:
                    request.session.pop(snapshot_key, None)
                    raise WechatError('微信通知配置已变化，请重新查询并核对现有地址用途')
                op, msg = perform_operation(channel, None, request.user, data['operation_id'],
                    'notify:' + data['action'], {'url': data['url']} if data['action'] != 'DELETE' else {})
                (messages.success if op.state == 'success' else messages.error)(request, msg)
                request.session.pop(snapshot_key, None)
                if op.state == 'success':
                    try: remote = query_remote(gw)
                    except WechatError: messages.info(request, '微信已接受操作，配置回查暂未成功，请再次查询')
                form = NotificationForm(merchant=channel, initial={'url': data['url']})
        except WechatError as error: form.add_error(None, str(error))
    return render(request, 'desk/channel_notifications.html', {**context(), 'menu': 'merchants',
        'channel': channel, 'form': form, 'remote': remote, 'jobs': channel.jobs.order_by('-id')[:10]})


@login_required
@permission_required('desk.view_wechatchannel', raise_exception=True)
@permission_required('desk.sync_complaint', raise_exception=True)
@require_POST
def retry_channel_job(request, channel_id, job_id):
    job = get_object_or_404(Job, pk=job_id, channel_id=channel_id, state='failed')
    Job.objects.get_or_create(dedupe_key=f'channel:{channel_id}:retry:{job.pk}',
        defaults={'channel': job.channel, 'kind': job.kind, 'payload': job.payload})
    messages.success(request, '渠道通知重试任务已提交')
    return redirect('channel_notifications', channel_id=channel_id)


@login_required
@require_POST
def upload(request, merchant_id, complaint_pk):
    if not (request.user.has_perm('desk.reply_complaint') or request.user.has_perm('desk.refund_complaint')):
        return JsonResponse({'error': '无图片上传权限'}, status=403)
    complaint = get_object_or_404(Complaint, pk=complaint_pk, merchant_id=merchant_id)
    file = request.FILES.get('file')
    if not file or file.size > 2 * 1024 * 1024: return JsonResponse({'error': '请选择不超过2MB的图片'}, status=400)
    try:
        mid = gateway_for(complaint.merchant).upload(file.name, file.read())
        Media.objects.create(merchant=complaint.merchant, complaint=complaint, media_id=mid)
        return JsonResponse({'media_id': mid})
    except WechatError as error: return JsonResponse({'error': str(error)}, status=400)


@login_required
@permission_required('desk.view_complaint', raise_exception=True)
@require_GET
def image(request, merchant_id, complaint_pk):
    complaint = get_object_or_404(Complaint, pk=complaint_pk, merchant_id=merchant_id)
    mid = request.GET.get('id', '')
    if mid not in allowed_media(complaint): raise Http404()
    try:
        raw, mime = gateway_for(complaint.merchant).download(mid)
        response = HttpResponse(raw, content_type=mime)
        response['Cache-Control'] = 'private, no-store'
        return response
    except WechatError: return HttpResponse('图片读取失败', status=502)


@csrf_exempt
@require_POST
def callback(request, mch_id):
    merchant = get_object_or_404(Merchant, mch_id=mch_id, is_demo=False, access_mode='direct')
    if len(request.body) > 1024 * 1024: return JsonResponse({'code': 'FAIL', 'message': '包体超过限制'}, status=413)
    try:
        verify_signature(merchant, request.headers, request.body)
        event_id, data = decrypt_notification(merchant, request.body)
        receive_notification(merchant, event_id, data)
    except WechatError: return JsonResponse({'code': 'FAIL', 'message': '验签或解密失败'}, status=400)
    except DatabaseError: return JsonResponse({'code': 'FAIL', 'message': '通知暂未可靠保存'}, status=503)
    return HttpResponse(status=204)


@csrf_exempt
@require_POST
def channel_callback(request, mch_id):
    channel = get_object_or_404(WechatChannel, mch_id=mch_id)
    if len(request.body) > 1024 * 1024: return JsonResponse({'code': 'FAIL', 'message': '包体超过限制'}, status=413)
    try:
        verify_signature(channel, request.headers, request.body)
        event_id, data = decrypt_notification(channel, request.body, check_merchant=False)
        receive_channel_notification(channel, event_id, data)
    except WechatError: return JsonResponse({'code': 'FAIL', 'message': '验签或解密失败'}, status=400)
    except DatabaseError: return JsonResponse({'code': 'FAIL', 'message': '通知暂未可靠保存'}, status=503)
    return HttpResponse(status=204)


@login_required
@permission_required('desk.view_operation', raise_exception=True)
def operations(request):
    rows = Operation.objects.select_related('merchant', 'channel', 'complaint', 'user')
    if request.GET.get('state') in ('success', 'failed', 'unknown', 'pending'): rows = rows.filter(state=request.GET['state'])
    return render(request, 'desk/operations.html', {**context(), 'menu': 'operations',
                  'page': Paginator(rows, 30).get_page(request.GET.get('page'))})


@login_required
@permission_required('desk.change_operation', raise_exception=True)
def resolve_operation(request, operation_id):
    operation = get_object_or_404(Operation, pk=operation_id)
    if request.method == 'POST':
        if operation.state not in ('pending', 'unknown') or request.POST.get('outcome') not in ('success', 'failed'):
            return HttpResponse('操作状态不允许确认', status=400)
        if ((operation.state == 'pending' or (not operation.complaint_id and operation.kind.startswith('notify:'))) and
            operation.created_at > timezone.now() - timedelta(minutes=5)):
            messages.error(request, '操作可能仍在执行，请等待至少5分钟后核对')
        elif operation.complaint and operation.complaint.synced_at < operation.created_at:
            messages.error(request, '请先同步该投诉的最新详情和协商记录，再确认远端结果')
        elif not request.POST.get('confirmed'):
            messages.error(request, '须确认已核对微信侧实际结果')
        else:
            operation.state = request.POST['outcome']
            operation.result = f'由 {request.user.get_username()} 核对远端并确认' + ('已执行' if operation.state == 'success' else '未执行')
            operation.save(update_fields=['state', 'result'])
            messages.success(request, '已记录人工核对结果')
        return redirect('operations')
    return render(request, 'desk/resolve.html', {**context(operation.merchant), 'operation': operation})


@require_GET
def health(request):
    # 对外仅检查数据库可用，不泄露商户信息。
    from django.db import connection
    try:
        with connection.cursor() as cur: cur.execute('SELECT 1')
    except DatabaseError: return JsonResponse({'status': 'unavailable'}, status=503)
    return JsonResponse({'status': 'ok'})


@login_required
@permission_required('desk.sync_complaint', raise_exception=True)
@require_POST
def retry_job(request, merchant_id, job_id):
    job = get_object_or_404(Job, pk=job_id, merchant_id=merchant_id, state='failed')
    enqueue(job.merchant, job.kind, job.payload, f'retry:{job.pk}')
    messages.success(request, '已提交重试任务')
    return redirect('workspace', merchant_id=merchant_id)
