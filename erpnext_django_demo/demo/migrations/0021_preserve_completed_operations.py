from django.db import migrations


def preserve_operations(apps, schema_editor):
    alias = schema_editor.connection.alias
    Batch = apps.get_model("demo", "FulfillmentBatch")
    BatchLine = apps.get_model("demo", "FulfillmentBatchLine")
    Charge = apps.get_model("demo", "InvoiceCharge")
    Invoice = apps.get_model("demo", "Invoice")
    Movement = apps.get_model("demo", "StockMovement")
    for marker in apps.get_model("demo", "Fulfillment").objects.using(alias).select_related("order"):
        batch = Batch.objects.using(alias).create(order_id=marker.order_id, legacy=True,
                                                 created_at=marker.completed_at)
        for line in marker.order.lines.using(alias).select_related("item"):
            BatchLine.objects.using(alias).create(batch=batch, order_line_id=line.pk,
                quantity=line.quantity, unit_price=line.unit_price,
                unit_cost=line.item.purchase_price if marker.order.kind == "sales" else line.unit_price)
        Movement.objects.using(alias).filter(order_id=marker.order_id).update(fulfillment_batch=batch)
        invoice = Invoice.objects.using(alias).filter(order_id=marker.order_id).first()
        if invoice:
            Charge.objects.using(alias).create(invoice=invoice, batch=batch, amount=invoice.amount,
                                               created_at=invoice.issued_at)
    Production = apps.get_model("demo", "ProductionBatch")
    Material = apps.get_model("demo", "ProductionBatchMaterial")
    for work in apps.get_model("demo", "WorkOrder").objects.using(alias).filter(status="completed"):
        batch = Production.objects.using(alias).create(work_order=work, legacy=True,
            accepted_quantity=work.quantity, created_at=work.completed_at,
            created_by_id=work.completed_by_id, quality_reason="سابقهٔ تکمیل کامل پیش از ثبت نوبتی")
        for row in work.materials.using(alias).all():
            Material.objects.using(alias).create(batch=batch, work_order_material=row,
                quantity=row.required_quantity, unit_cost=row.unit_cost)
        Movement.objects.using(alias).filter(work_order=work).update(production_batch=batch)


class Migration(migrations.Migration):
    dependencies = [("demo", "0020_fulfillmentbatch_stockmovement_fulfillment_batch_and_more")]
    operations = [migrations.RunPython(preserve_operations, migrations.RunPython.noop)]
