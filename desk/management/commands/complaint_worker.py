import signal
import time
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone
from desk.models import Merchant
from desk.services import enqueue, process_job
from desk.wechat import configuration_errors


class Command(BaseCommand):
    help = '消费MySQL持久同步任务；每10分钟补拉最近2天投诉（单次模式仅清空当前队列）'

    def add_arguments(self, parser): parser.add_argument('--once', action='store_true')

    def handle(self, *args, **options):
        running, last_schedule = True, 0
        def stop(*_):
            nonlocal running
            running = False
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while running:
            close_old_connections()
            if not options['once'] and time.monotonic() - last_schedule >= 600:
                today = timezone.localdate()
                for merchant in Merchant.objects.filter(enabled=True):
                    if not configuration_errors(merchant):
                        enqueue(merchant, 'sync', {'begin': (today - timedelta(days=2)).isoformat(), 'end': today.isoformat()}, 'scheduled')
                last_schedule = time.monotonic()
            did_work = process_job()
            if not did_work:
                if options['once']: break
                time.sleep(1)
        self.stdout.write('同步工作进程已退出')
