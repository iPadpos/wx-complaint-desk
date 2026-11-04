from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.forms import AdminPasswordChangeForm
from django.contrib.auth.models import Group, User
from .models import Complaint, Job, Operation


class AccountPasswordForm(AdminPasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['password2'].help_text = ''
        if 'usable_password' in self.fields:
            self.fields['usable_password'].help_text = (
                '<ul id="id_unusable_warning" class="messagelist">'
                '<li class="warning">禁用将清除当前密码。</li></ul>')


admin.site.unregister(User)
admin.site.unregister(Group)


@admin.register(User)
class AccountAdmin(UserAdmin):
    change_password_form = AccountPasswordForm

    class Media:
        js = ['admin-concise.js']

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        for name, field in form.base_fields.items():
            if name != 'password1':
                field.help_text = ''
        for name, label in {'is_active': '账号启用', 'is_staff': '可登录账号管理',
                            'is_superuser': '超级用户（全部权限）'}.items():
            if name in form.base_fields:
                form.base_fields[name].label = label
        return form


@admin.register(Group)
class PermissionGroupAdmin(GroupAdmin):
    class Media:
        js = ['admin-concise.js']

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        form.base_fields['permissions'].help_text = ''
        return form


@admin.register(Complaint)
class ComplaintAdmin(admin.ModelAdmin):
    class Media:
        css = {'all': ['admin-detail.css']}

    list_display = ['complaint_id', 'merchant', 'state', 'complaint_time']
    list_filter = ['merchant', 'state']
    exclude = ['detail', 'history']
    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False


@admin.register(Job, Operation)
class ReadOnlyAdmin(admin.ModelAdmin):
    class Media:
        css = {'all': ['admin-detail.css']}

    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False


admin.site.site_header = '客诉系统 · 账号与权限'
admin.site.site_title = '客诉系统'
