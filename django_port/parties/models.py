from collections import defaultdict

from django.core.exceptions import ValidationError
from django.db import models, transaction


class TreeQuerySet(models.QuerySet):
    def update(self, **kwargs):
        structural = {"name", "is_group", self.model.parent_field, f"{self.model.parent_field}_id"}
        if structural & kwargs.keys():
            raise ValidationError("Change tree structure through model.save().")
        return super().update(**kwargs)

    def delete(self):
        total = 0
        details = defaultdict(int)
        with transaction.atomic():
            for node in list(self.order_by("-lft")):
                count, per_model = node.delete()
                total += count
                for model, deleted in per_model.items():
                    details[model] += deleted
        return total, dict(details)


class TreeNode(models.Model):
    name = models.CharField(max_length=140, primary_key=True)
    is_group = models.BooleanField(default=False)
    lft = models.PositiveIntegerField(default=0, editable=False, db_index=True)
    rgt = models.PositiveIntegerField(default=0, editable=False, db_index=True)

    objects = TreeQuerySet.as_manager()

    class Meta:
        abstract = True
        ordering = ("lft", "name")

    def clean(self):
        super().clean()
        parent_id = getattr(self, f"{self.parent_field}_id")
        if parent_id:
            if parent_id == self.name:
                raise ValidationError({self.parent_field: "A node cannot be its own parent."})
            parent = getattr(self, self.parent_field)
            if not parent.is_group:
                raise ValidationError({self.parent_field: "Parent must be a group."})
            seen = {self.name}
            while parent:
                if parent.name in seen:
                    raise ValidationError({self.parent_field: "Tree cannot contain a cycle."})
                seen.add(parent.name)
                parent = getattr(parent, parent.parent_field)
        else:
            if type(self).objects.exclude(pk=self.pk).filter(**{f"{self.parent_field}__isnull": True}).exists():
                raise ValidationError({self.parent_field: "Only one root is allowed."})
            if not self.is_group:
                raise ValidationError({"is_group": "Root must be a group."})
        if not self.is_group and self.pk and self.children.exists():
            raise ValidationError({"is_group": "A node with children must remain a group."})

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if not getattr(self, f"{self.parent_field}_id"):
                root = type(self).objects.filter(**{f"{self.parent_field}__isnull": True}).exclude(pk=self.pk).first()
                if root:
                    setattr(self, self.parent_field, root)
                    if kwargs.get("update_fields") is not None:
                        kwargs["update_fields"] = set(kwargs["update_fields"]) | {self.parent_field}
            self.full_clean()
            result = super().save(*args, **kwargs)
            rebuild_tree(type(self))
            self.refresh_from_db(fields=["lft", "rgt"])
            return result

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            if self.children.exists():
                raise ValidationError("Cannot delete a node with children.")
            result = super().delete(*args, **kwargs)
            rebuild_tree(type(self))
            return result

    def descendants(self, include_self=False):
        if include_self:
            return type(self).objects.filter(lft__gte=self.lft, rgt__lte=self.rgt).order_by("lft")
        return type(self).objects.filter(lft__gt=self.lft, rgt__lt=self.rgt).order_by("lft")

    def __str__(self):
        return self.name


def rebuild_tree(model):
    nodes = list(model.objects.select_for_update().order_by("name"))
    if not nodes:
        return
    parent_key = f"{model.parent_field}_id"
    roots = [node for node in nodes if getattr(node, parent_key) is None]
    if len(roots) != 1:
        raise ValidationError("Tree must have one root.")
    children = defaultdict(list)
    for node in nodes:
        children[getattr(node, parent_key)].append(node)
    seen = set()
    counter = 0

    def visit(node):
        nonlocal counter
        if node.pk in seen:
            raise ValidationError("Tree cannot contain a cycle.")
        seen.add(node.pk)
        counter += 1
        node.lft = counter
        for child in children[node.pk]:
            visit(child)
        counter += 1
        node.rgt = counter

    visit(roots[0])
    if len(seen) != len(nodes):
        raise ValidationError("Tree contains disconnected nodes.")
    model.objects.bulk_update(nodes, ["lft", "rgt"])


class CustomerGroup(TreeNode):
    parent_field = "parent_customer_group"
    parent_customer_group = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children"
    )
    default_price_list = models.ForeignKey(
        "catalog.PriceList", null=True, blank=True, on_delete=models.PROTECT,
        related_name="customer_groups",
    )

    class Meta(TreeNode.Meta):
        db_table = "customer_group"


class SupplierGroup(TreeNode):
    parent_field = "parent_supplier_group"
    parent_supplier_group = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children"
    )

    class Meta(TreeNode.Meta):
        db_table = "supplier_group"


class Territory(TreeNode):
    parent_field = "parent_territory"
    parent_territory = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children"
    )

    class Meta(TreeNode.Meta):
        db_table = "territory"


class PartyType(models.TextChoices):
    COMPANY = "Company", "Company"
    INDIVIDUAL = "Individual", "Individual"
    PARTNERSHIP = "Partnership", "Partnership"


class PartyDisplayMixin:
    primary_contact_field = None
    primary_address_field = None

    def _live_primary_contact(self):
        contact_id = getattr(self, f"{self.primary_contact_field}_id")
        if not contact_id:
            return None
        from contacts.models import Contact

        return Contact.objects.filter(pk=contact_id).first()

    @property
    def email_id(self):
        contact = self._live_primary_contact()
        return contact.email_id if contact else ""

    @property
    def mobile_no(self):
        contact = self._live_primary_contact()
        return contact.mobile_no if contact else ""

    @property
    def primary_address(self):
        address_id = getattr(self, f"{self.primary_address_field}_id")
        if not address_id:
            return ""
        from contacts.models import Address

        address = Address.objects.filter(pk=address_id).first()
        return address.display if address else ""


class Customer(PartyDisplayMixin, models.Model):
    primary_contact_field = "customer_primary_contact"
    primary_address_field = "customer_primary_address"
    name = models.CharField(max_length=140, primary_key=True)
    customer_name = models.CharField(max_length=140)
    customer_type = models.CharField(max_length=20, choices=PartyType.choices, default=PartyType.COMPANY)
    customer_group = models.ForeignKey(CustomerGroup, null=True, blank=True, on_delete=models.PROTECT, related_name="customers")
    territory = models.ForeignKey(Territory, null=True, blank=True, on_delete=models.PROTECT, related_name="customers")
    disabled = models.BooleanField(default=False)
    is_internal_customer = models.BooleanField(default=False)
    represents_company = models.ForeignKey("organizations.Company", null=True, blank=True, on_delete=models.PROTECT, related_name="internal_customers")
    default_currency = models.ForeignKey("geo.Currency", null=True, blank=True, on_delete=models.PROTECT, related_name="customers")
    default_price_list = models.ForeignKey("catalog.PriceList", null=True, blank=True, on_delete=models.PROTECT, related_name="customers")
    customer_primary_address = models.ForeignKey("contacts.Address", null=True, blank=True, on_delete=models.PROTECT, related_name="primary_for_customers")
    customer_primary_contact = models.ForeignKey("contacts.Contact", null=True, blank=True, on_delete=models.PROTECT, related_name="primary_for_customers")
    tax_id = models.CharField(max_length=140, blank=True)
    website = models.CharField(max_length=140, blank=True)
    customer_details = models.TextField(blank=True)
    on_hold = models.BooleanField(default=False)
    release_date = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "customer"

    @property
    def first_name(self):
        contact = self._live_primary_contact()
        return contact.first_name if contact else ""

    @property
    def last_name(self):
        contact = self._live_primary_contact()
        return contact.last_name if contact else ""

    def clean(self):
        super().clean()
        self.customer_name = self.customer_name.strip()
        if not self.customer_name:
            raise ValidationError({"customer_name": "Customer name is required."})
        if self.customer_group_id and self.customer_group.is_group:
            raise ValidationError({"customer_group": "Select a leaf customer group."})
        if self.default_price_list_id and not self.default_price_list.selling:
            raise ValidationError({"default_price_list": "Select a selling price list."})
        if self.customer_primary_address_id:
            address = self.customer_primary_address
            if address.disabled or not address.party_links.filter(customer_id=self.name).exists():
                raise ValidationError({"customer_primary_address": "Select an enabled address linked to this customer."})
        if self.customer_primary_contact_id:
            if not self.customer_primary_contact.party_links.filter(customer_id=self.name).exists():
                raise ValidationError({"customer_primary_contact": "Select a contact linked to this customer."})
        if not self.is_internal_customer:
            self.represents_company = None
        if not self.on_hold:
            self.release_date = None

    def save(self, *args, **kwargs):
        if not self.name:
            self.name = self.customer_name.strip()
        with transaction.atomic():
            self.full_clean()
            result = super().save(*args, **kwargs)
            if self.customer_primary_address_id:
                self.customer_primary_address.__class__.objects.filter(pk=self.customer_primary_address_id).update(is_primary_address=True)
            if self.customer_primary_contact_id:
                self.customer_primary_contact.__class__.objects.filter(pk=self.customer_primary_contact_id).update(is_primary_contact=True)
            return result

    def __str__(self):
        return self.customer_name


class Supplier(PartyDisplayMixin, models.Model):
    primary_contact_field = "supplier_primary_contact"
    primary_address_field = "supplier_primary_address"
    class HoldType(models.TextChoices):
        ALL = "All", "All"
        INVOICES = "Invoices", "Invoices"
        PAYMENTS = "Payments", "Payments"

    name = models.CharField(max_length=140, primary_key=True)
    supplier_name = models.CharField(max_length=140)
    supplier_type = models.CharField(max_length=20, choices=PartyType.choices, default=PartyType.COMPANY)
    supplier_group = models.ForeignKey(SupplierGroup, null=True, blank=True, on_delete=models.PROTECT, related_name="suppliers")
    country = models.ForeignKey("geo.Country", null=True, blank=True, on_delete=models.PROTECT, related_name="suppliers")
    disabled = models.BooleanField(default=False)
    is_internal_supplier = models.BooleanField(default=False)
    represents_company = models.ForeignKey("organizations.Company", null=True, blank=True, on_delete=models.PROTECT, related_name="internal_suppliers")
    default_currency = models.ForeignKey("geo.Currency", null=True, blank=True, on_delete=models.PROTECT, related_name="suppliers")
    default_price_list = models.ForeignKey("catalog.PriceList", null=True, blank=True, on_delete=models.PROTECT, related_name="suppliers")
    supplier_primary_address = models.ForeignKey("contacts.Address", null=True, blank=True, on_delete=models.PROTECT, related_name="primary_for_suppliers")
    supplier_primary_contact = models.ForeignKey("contacts.Contact", null=True, blank=True, on_delete=models.PROTECT, related_name="primary_for_suppliers")
    tax_id = models.CharField(max_length=140, blank=True)
    website = models.CharField(max_length=140, blank=True)
    supplier_details = models.TextField(blank=True)
    on_hold = models.BooleanField(default=False)
    hold_type = models.CharField(max_length=10, choices=HoldType.choices, default=HoldType.ALL)
    release_date = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "supplier"

    def clean(self):
        super().clean()
        self.supplier_name = self.supplier_name.strip()
        if not self.supplier_name:
            raise ValidationError({"supplier_name": "Supplier name is required."})
        if self.default_price_list_id and not self.default_price_list.buying:
            raise ValidationError({"default_price_list": "Select a buying price list."})
        if self.supplier_primary_address_id:
            address = self.supplier_primary_address
            if address.disabled or not address.party_links.filter(supplier_id=self.name).exists():
                raise ValidationError({"supplier_primary_address": "Select an enabled address linked to this supplier."})
        if self.supplier_primary_contact_id:
            if not self.supplier_primary_contact.party_links.filter(supplier_id=self.name).exists():
                raise ValidationError({"supplier_primary_contact": "Select a contact linked to this supplier."})
        if not self.is_internal_supplier:
            self.represents_company = None
        if not self.on_hold:
            self.release_date = None

    def save(self, *args, **kwargs):
        if not self.name:
            self.name = self.supplier_name.strip()
        with transaction.atomic():
            self.full_clean()
            result = super().save(*args, **kwargs)
            if self.supplier_primary_address_id:
                self.supplier_primary_address.__class__.objects.filter(pk=self.supplier_primary_address_id).update(is_primary_address=True)
            if self.supplier_primary_contact_id:
                self.supplier_primary_contact.__class__.objects.filter(pk=self.supplier_primary_contact_id).update(is_primary_contact=True)
            return result

    def __str__(self):
        return self.supplier_name
