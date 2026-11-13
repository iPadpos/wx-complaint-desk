from django.contrib.auth.models import Permission, User
from django.test import TestCase

from .models import Merchant


class MerchantSortTests(TestCase):
    def setUp(self):
        self.a = Merchant.objects.create(name='甲商户', mch_id='1990000301', is_demo=True)
        self.b = Merchant.objects.create(name='乙商户', mch_id='1990000302', is_demo=True)
        self.c = Merchant.objects.create(name='丙商户', mch_id='1990000303', is_demo=True)
        self.admin = User.objects.create_superuser('sort-admin', '', 'LocalTestOnly!9834')

    def names(self):
        return list(Merchant.objects.values_list('name', flat=True))

    def test_sort_order_first_then_unsorted_by_id(self):
        self.assertEqual(self.names(), ['甲商户', '乙商户', '丙商户'])
        Merchant.objects.filter(pk=self.c.pk).update(sort_order=1)
        Merchant.objects.filter(pk=self.a.pk).update(sort_order=5)
        self.assertEqual(self.names(), ['丙商户', '甲商户', '乙商户'])

    def test_reorder_saves_full_order_and_sidebar_follows(self):
        self.client.force_login(self.admin)
        response = self.client.post('/merchants/reorder/', {'ids': [self.c.pk, self.a.pk, self.b.pk]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.names(), ['丙商户', '甲商户', '乙商户'])
        self.assertEqual(list(Merchant.objects.values_list('sort_order', flat=True)), [1, 2, 3])
        page = self.client.get('/merchants/').content.decode()
        self.assertLess(page.index('丙商户'), page.index('甲商户'))
        self.assertIn('data-sortable="/merchants/reorder/"', page)
        self.assertIn('draggable="true"', page)
        searched = self.client.get('/merchants/', {'q': '甲'}).content.decode()
        self.assertNotIn('draggable="true"', searched)

    def test_reorder_rejects_stale_or_invalid_lists(self):
        self.client.force_login(self.admin)
        for ids in ([self.a.pk, self.b.pk], [self.a.pk, self.b.pk, self.c.pk, 999999],
                    [self.a.pk, self.a.pk, self.b.pk, self.c.pk]):
            self.assertEqual(self.client.post('/merchants/reorder/', {'ids': ids}).status_code, 409)
        self.assertEqual(self.client.post('/merchants/reorder/', {'ids': ['x']}).status_code, 400)
        self.assertEqual(self.client.get('/merchants/reorder/').status_code, 405)
        self.assertEqual(list(Merchant.objects.values_list('sort_order', flat=True)), [None, None, None])

    def test_reorder_requires_change_permission(self):
        reader = User.objects.create_user('sort-reader', password='LocalTestOnly!9834')
        reader.user_permissions.add(Permission.objects.get(codename='view_merchant'),
                                    Permission.objects.get(codename='view_complaint'))
        self.client.force_login(reader)
        response = self.client.post('/merchants/reorder/', {'ids': [self.c.pk, self.a.pk, self.b.pk]})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.names(), ['甲商户', '乙商户', '丙商户'])
        self.assertNotIn('data-sortable', self.client.get('/merchants/').content.decode())

    def test_reorder_enforces_csrf(self):
        self.client = self.client_class(enforce_csrf_checks=True)
        self.client.force_login(self.admin)
        response = self.client.post('/merchants/reorder/', {'ids': [self.c.pk, self.a.pk, self.b.pk]})
        self.assertEqual(response.status_code, 403)

    def test_edit_form_saves_sort_order(self):
        self.client.force_login(self.admin)
        page = self.client.get(f'/merchants/{self.b.pk}/edit/').content.decode()
        self.assertIn('name="sort_order"', page)
