from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q


class Address(models.Model):
    class Type(models.TextChoices):
        BILLING = "Billing", "Billing"
        SHIPPING = "Shipping", "Shipping"
        OFFICE = "Office", "Office"
        PERSONAL = "Personal", "Personal"
        PLANT = "Plant", "Plant"
        POSTAL = "Postal", "Postal"
        SHOP = "Shop", "Shop"
        SUBSIDIARY = "Subsidiary", "Subsidiary"
        WAREHOUSE = "Warehouse", "Warehouse"
        CURRENT = "Current", "Current"
        PERMANENT = "Permanent", "Permanent"
        OTHER = "Other", "Other"

    name = models.CharField(max_length=140, primary_key=True)
    address_title = models.CharField(max_length=140, blank=True)
    address_type = models.CharField(max_length=20, choices=Type.choices)
    address_line1 = models.CharField(max_length=240)
    address_line2 = models.CharField(max_length=240, blank=True)
    city = models.CharField(max_length=140)
    county = models.CharField(max_length=140, blank=True)
    state = models.CharField(max_length=140, blank=True)
    country = models.ForeignKey("geo.Country", on_delete=models.PROTECT, related_name="addresses")
    pincode = models.CharField(max_length=140, blank=True)
    email_id = models.EmailField(blank=True)
    phone = models.CharField(max_length=140, blank=True)
    fax = models.CharField(max_length=140, blank=True)
    is_primary_address = models.BooleanField(default=False)
    is_shipping_address = models.BooleanField(default=False)
    disabled = models.BooleanField(default=False)

    class Meta:
        db_table = "address"
        verbose_name_plural = "addresses"

    def clean(self):
        super().clean()
        if self.disabled and self.pk:
            if self.primary_for_customers.exists() or self.primary_for_suppliers.exists():
                raise ValidationError({"disabled": "A primary party address cannot be disabled."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.address_title or self.name

    @property
    def display(self):
        locality = ", ".join(part for part in (self.city, self.state, self.pincode) if part)
        return "\n".join(part for part in (
            self.address_line1, self.address_line2, locality, self.country_id,
        ) if part)


class Contact(models.Model):
    class Status(models.TextChoices):
        PASSIVE = "Passive", "Passive"
        OPEN = "Open", "Open"
        REPLIED = "Replied", "Replied"

    name = models.CharField(max_length=140, primary_key=True)
    first_name = models.CharField(max_length=140, blank=True)
    middle_name = models.CharField(max_length=140, blank=True)
    last_name = models.CharField(max_length=140, blank=True)
    company_name = models.CharField(max_length=140, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PASSIVE)
    designation = models.CharField(max_length=140, blank=True)
    department = models.CharField(max_length=140, blank=True)
    address = models.ForeignKey(Address, null=True, blank=True, on_delete=models.SET_NULL, related_name="contacts")
    is_primary_contact = models.BooleanField(default=False)
    unsubscribed = models.BooleanField(default=False)

    class Meta:
        db_table = "contact"

    @property
    def email_id(self):
        primary = self.email_ids.filter(is_primary=True).first()
        return primary.email_id if primary else ""

    @property
    def phone(self):
        primary = self.phone_nos.filter(is_primary_phone=True).first()
        return primary.phone if primary else ""

    @property
    def mobile_no(self):
        primary = self.phone_nos.filter(is_primary_mobile_no=True).first()
        return primary.phone if primary else ""

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return " ".join(part for part in (self.first_name, self.middle_name, self.last_name) if part) or self.company_name or self.name

    @property
    def full_name(self):
        return str(self)


class ContactEmailQuerySet(models.QuerySet):
    def delete(self):
        count = 0
        details = {}
        with transaction.atomic():
            for email in self:
                deleted, per_model = email.delete()
                count += deleted
                for model, value in per_model.items():
                    details[model] = details.get(model, 0) + value
        return count, details


class ContactEmail(models.Model):
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="email_ids")
    email_id = models.EmailField()
    is_primary = models.BooleanField(default=False)

    objects = ContactEmailQuerySet.as_manager()

    class Meta:
        db_table = "contact_email"
        constraints = [models.UniqueConstraint(fields=("contact",), condition=Q(is_primary=True), name="one_primary_email_per_contact")]

    def save(self, *args, **kwargs):
        if not ContactEmail.objects.filter(contact_id=self.contact_id).exclude(pk=self.pk).exists():
            self.is_primary = True
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {"is_primary"}
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            contact_id = self.contact_id
            result = super().delete(*args, **kwargs)
            remaining = ContactEmail.objects.filter(contact_id=contact_id)
            if remaining.count() == 1:
                remaining.update(is_primary=True)
            return result

    def __str__(self):
        return self.email_id


class ContactPhone(models.Model):
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="phone_nos")
    phone = models.CharField(max_length=140)
    is_primary_phone = models.BooleanField(default=False)
    is_primary_mobile_no = models.BooleanField(default=False)

    class Meta:
        db_table = "contact_phone"
        constraints = [
            models.UniqueConstraint(fields=("contact",), condition=Q(is_primary_phone=True), name="one_primary_phone_per_contact"),
            models.UniqueConstraint(fields=("contact",), condition=Q(is_primary_mobile_no=True), name="one_primary_mobile_per_contact"),
        ]

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.phone


class PartyLinkQuerySet(models.QuerySet):
    def update(self, **kwargs):
        structural = {"customer", "customer_id", "supplier", "supplier_id", "address", "address_id", "contact", "contact_id"}
        if structural & kwargs.keys():
            raise ValidationError("Change party links through model.save().")
        return super().update(**kwargs)

    def delete(self):
        count = 0
        details = {}
        with transaction.atomic():
            for link in self:
                deleted, per_model = link.delete()
                count += deleted
                for model, value in per_model.items():
                    details[model] = details.get(model, 0) + value
        return count, details


class PartyLink(models.Model):
    customer = models.ForeignKey("parties.Customer", null=True, blank=True, on_delete=models.CASCADE)
    supplier = models.ForeignKey("parties.Supplier", null=True, blank=True, on_delete=models.CASCADE)

    objects = PartyLinkQuerySet.as_manager()

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if bool(self.customer_id) == bool(self.supplier_id):
            raise ValidationError("Select exactly one customer or supplier.")
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).first()
            if old and (
                old.customer_id != self.customer_id
                or old.supplier_id != self.supplier_id
                or getattr(old, f"{self.record_field}_id") != getattr(self, f"{self.record_field}_id")
            ):
                old.ensure_not_primary()

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class AddressPartyLink(PartyLink):
    record_field = "address"
    address = models.ForeignKey(Address, on_delete=models.CASCADE, related_name="party_links")

    class Meta:
        db_table = "address_party_link"
        constraints = [
            models.CheckConstraint(condition=(Q(customer__isnull=False, supplier__isnull=True) | Q(customer__isnull=True, supplier__isnull=False)), name="address_link_one_party"),
            models.UniqueConstraint(fields=("address", "customer"), condition=Q(customer__isnull=False), name="unique_address_customer"),
            models.UniqueConstraint(fields=("address", "supplier"), condition=Q(supplier__isnull=False), name="unique_address_supplier"),
        ]

    def ensure_not_primary(self):
        if self.customer_id and self.customer.customer_primary_address_id == self.address_id:
            raise ValidationError("Cannot unlink a customer's primary address.")
        if self.supplier_id and self.supplier.supplier_primary_address_id == self.address_id:
            raise ValidationError("Cannot unlink a supplier's primary address.")

    def delete(self, *args, **kwargs):
        self.ensure_not_primary()
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.address_id}: {self.customer_id or self.supplier_id}"


class ContactPartyLink(PartyLink):
    record_field = "contact"
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="party_links")

    class Meta:
        db_table = "contact_party_link"
        constraints = [
            models.CheckConstraint(condition=(Q(customer__isnull=False, supplier__isnull=True) | Q(customer__isnull=True, supplier__isnull=False)), name="contact_link_one_party"),
            models.UniqueConstraint(fields=("contact", "customer"), condition=Q(customer__isnull=False), name="unique_contact_customer"),
            models.UniqueConstraint(fields=("contact", "supplier"), condition=Q(supplier__isnull=False), name="unique_contact_supplier"),
        ]

    def ensure_not_primary(self):
        if self.customer_id and self.customer.customer_primary_contact_id == self.contact_id:
            raise ValidationError("Cannot unlink a customer's primary contact.")
        if self.supplier_id and self.supplier.supplier_primary_contact_id == self.contact_id:
            raise ValidationError("Cannot unlink a supplier's primary contact.")

    def delete(self, *args, **kwargs):
        self.ensure_not_primary()
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.contact_id}: {self.customer_id or self.supplier_id}"
