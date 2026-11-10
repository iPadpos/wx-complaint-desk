import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone
from .fields import EncryptedJSONField


class WechatChannel(models.Model):
    name = models.CharField('渠道账号名称', max_length=80)
    provider = models.CharField('所属支付机构', max_length=80, blank=True)
    mch_id = models.CharField('微信渠道号', max_length=32, unique=True)
    enabled = models.BooleanField('启用渠道调用', default=True)
    serial_no = models.CharField('渠道证书序列号', max_length=128, blank=True)
    private_key_path = models.CharField('渠道私钥路径', max_length=512, blank=True)
    api_v3_key = EncryptedJSONField(default=str)
    public_key_id = models.CharField('微信公钥 ID／平台证书序列号', max_length=128, blank=True)
    public_key_path = models.CharField('微信公钥／平台证书路径', max_length=512, blank=True)
    secondary_key_id = models.CharField('备用验签 ID', max_length=128, blank=True)
    secondary_key_path = models.CharField('备用验签文件', max_length=512, blank=True)
    notify_url = models.URLField('公网 HTTPS 回调地址', max_length=512, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_demo = False

    class Meta:
        ordering = ['id']
        verbose_name = '微信渠道账号'
        verbose_name_plural = '微信渠道账号'

    @property
    def callback_path(self):
        return f'/callbacks/channels/{self.mch_id}/'

    @property
    def violation_callback_path(self):
        return f'/callbacks/channels/{self.mch_id}/violations/'

    def __str__(self):
        return f'{self.name} · {self.mch_id}'


class Merchant(models.Model):
    class AccessMode(models.TextChoices):
        DIRECT = 'direct', '商户自行调用'
        CHANNEL = 'channel', '渠道代调用'

    name = models.CharField('商户名称', max_length=80)
    mch_id = models.CharField('微信商户号', max_length=32, unique=True)
    access_mode = models.CharField('接入方式', max_length=16, choices=AccessMode.choices, default=AccessMode.DIRECT)
    channel = models.ForeignKey(WechatChannel, verbose_name='渠道账号', on_delete=models.PROTECT,
                               related_name='merchants', null=True, blank=True)
    provider_merchant_id = models.CharField('支付机构商户编号', max_length=64, blank=True)
    enabled = models.BooleanField('启用同步', default=True)
    is_demo = models.BooleanField('演示商户', default=False)
    serial_no = models.CharField('商户证书序列号', max_length=128, blank=True)
    private_key_path = models.CharField('商户私钥路径', max_length=512, blank=True)
    api_v3_key = EncryptedJSONField(default=str)
    public_key_id = models.CharField('微信公钥 ID／平台证书序列号', max_length=128, blank=True)
    public_key_path = models.CharField('微信公钥／平台证书路径', max_length=512, blank=True)
    secondary_key_id = models.CharField('备用验签 ID', max_length=128, blank=True)
    secondary_key_path = models.CharField('备用验签文件', max_length=512, blank=True)
    notify_url = models.URLField('公网 HTTPS 回调地址', max_length=512, blank=True)
    allowed_jump_hosts = models.CharField('允许回复链接的域名', max_length=512, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_sync_error = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']
        verbose_name = '商户'
        verbose_name_plural = '商户'
        constraints = [models.CheckConstraint(condition=(
            models.Q(access_mode='direct', channel__isnull=True) |
            models.Q(access_mode='channel', channel__isnull=False, is_demo=False)),
            name='merchant_access_mode_valid')]

    @property
    def callback_path(self):
        return f'/callbacks/{self.mch_id}/'

    def __str__(self):
        return f'{self.name} · {self.mch_id}'


class Complaint(models.Model):
    STATES = [('PENDING', '待处理'), ('PROCESSING', '处理中'), ('PROCESSED', '已完成')]
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name='complaints')
    complaint_id = models.CharField(max_length=64)
    state = models.CharField(max_length=30, choices=STATES, default='PENDING')
    problem_type = models.CharField(max_length=64, blank=True)
    order_no = models.CharField(max_length=128, blank=True, db_index=True)
    complaint_time = models.DateTimeField(default=timezone.now)
    incoming_user_response = models.BooleanField(default=False)
    need_immediate_service = models.BooleanField(default=False)
    in_platform_service = models.BooleanField(default=False)
    detail = EncryptedJSONField(default=dict)
    history = EncryptedJSONField(default=list)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-complaint_time', '-id']
        constraints = [models.UniqueConstraint(fields=['merchant', 'complaint_id'], name='merchant_complaint_unique')]
        indexes = [models.Index(fields=['merchant', 'state', 'complaint_time'])]
        permissions = [
            ('sync_complaint', '同步微信投诉'), ('reply_complaint', '回复微信投诉'),
            ('complete_complaint', '反馈处理完成'), ('refund_complaint', '反馈退款审批'),
            ('configure_notification', '管理微信通知地址'),
        ]

    def __str__(self):
        return self.complaint_id


class Job(models.Model):
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name='jobs', null=True, blank=True)
    channel = models.ForeignKey(WechatChannel, on_delete=models.PROTECT, related_name='jobs', null=True, blank=True)
    kind = models.CharField(max_length=20, default='sync')
    payload = models.JSONField(default=dict)
    dedupe_key = models.CharField(max_length=200, unique=True, null=True, blank=True)
    state = models.CharField(max_length=20, default='queued')
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now, db_index=True)
    locked_at = models.DateTimeField(null=True)
    error = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=(
            models.Q(merchant__isnull=False, channel__isnull=True) |
            models.Q(merchant__isnull=True, channel__isnull=False)), name='job_owner_valid')]


class ChannelNotification(models.Model):
    channel = models.ForeignKey(WechatChannel, on_delete=models.PROTECT)
    event_id = models.CharField(max_length=100)
    payload = EncryptedJSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['channel', 'event_id'], name='channel_event_unique')]


class ViolationRecord(models.Model):
    # 商户平台处置通知：渠道下全部子商户都会推送，按子商户号关联已接入商户。
    EVENT_TYPES = [('VIOLATION.PUNISH', '处罚'), ('VIOLATION.INTERCEPT', '拦截'), ('VIOLATION.APPEAL', '申诉')]
    channel = models.ForeignKey(WechatChannel, on_delete=models.PROTECT, related_name='violations')
    event_id = models.CharField(max_length=100)
    event_type = models.CharField(max_length=32, choices=EVENT_TYPES)
    sub_mchid = models.CharField(max_length=32)
    record_id = models.CharField(max_length=128)
    punish_time = models.DateTimeField(null=True, blank=True)
    payload = EncryptedJSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [models.UniqueConstraint(fields=['channel', 'event_id'], name='violation_event_unique')]
        indexes = [models.Index(fields=['channel', 'sub_mchid', 'created_at'])]


class Notification(models.Model):
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT)
    event_id = models.CharField(max_length=100)
    payload = EncryptedJSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['merchant', 'event_id'], name='merchant_event_unique')]


class Operation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, null=True, blank=True)
    channel = models.ForeignKey(WechatChannel, on_delete=models.PROTECT, null=True, blank=True)
    complaint = models.ForeignKey(Complaint, null=True, on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    kind = models.CharField(max_length=30)
    payload_hash = models.CharField(max_length=64)
    state = models.CharField(max_length=20, default='pending')
    request_id = models.CharField(max_length=128, blank=True)
    result = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [models.CheckConstraint(condition=(
            models.Q(merchant__isnull=False, channel__isnull=True) |
            models.Q(merchant__isnull=True, channel__isnull=False, complaint__isnull=True)),
            name='operation_owner_valid')]

    @property
    def owner(self):
        return self.channel if self.channel_id else self.merchant


class Media(models.Model):
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT)
    complaint = models.ForeignKey(Complaint, on_delete=models.PROTECT)
    media_id = models.CharField(max_length=512)
    created_at = models.DateTimeField(auto_now_add=True)
