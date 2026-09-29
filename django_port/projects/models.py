from django.core.exceptions import ValidationError
from django.db import models


class Project(models.Model):
    class Status(models.TextChoices):
        OPEN = "Open", "Open"
        ON_HOLD = "On hold", "On hold"
        COMPLETED = "Completed", "Completed"
        CANCELLED = "Cancelled", "Cancelled"

    name = models.CharField(max_length=140, primary_key=True)
    project_name = models.CharField(max_length=140, unique=True)
    company = models.ForeignKey("organizations.Company", on_delete=models.PROTECT, related_name="projects")
    customer = models.ForeignKey("parties.Customer", null=True, blank=True, on_delete=models.PROTECT, related_name="projects")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    is_active = models.BooleanField(default=True)
    expected_start_date = models.DateField(null=True, blank=True)
    expected_end_date = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "project"
        ordering = ("project_name", "name")

    def clean(self):
        super().clean()
        self.name = (self.name or "").strip()
        self.project_name = (self.project_name or "").strip()
        if not self.name or not self.project_name:
            raise ValidationError("Project ID and name are required.")
        if self.expected_start_date and self.expected_end_date and self.expected_start_date > self.expected_end_date:
            raise ValidationError({"expected_end_date": "End date must not be before start date."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.project_name
