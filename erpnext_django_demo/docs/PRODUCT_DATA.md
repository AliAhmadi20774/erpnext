# قرارداد دادهٔ نسخه‌پذیر کالا و BOM

این بسته، ساختار محصول را از دادهٔ عملیاتی جدا می‌کند تا master data کالا و BOM چندسطحی مانند کد بازبینی، نسخه‌گذاری و بین محیط‌ها منتقل شود.

## فایل‌های قابل ثبت در Git

- `data/product_structure.v1.schema.json`: قرارداد JSON Schema نسخهٔ ۱؛ مناسب IDE، CI و ابزارهای یکپارچه‌سازی.
- `data/demo_product_structure.v1.json`: دادهٔ واقعی «ایستگاه کاری سازمانی» شامل ۷ کالا، یک زیرمونتاژ و ۲ BOM فعال.

سند داده چهار بخش دارد:

```json
{
  "schema_version": "1.0",
  "dataset": {"code": "...", "name": "...", "description": "..."},
  "items": [{"sku": "...", "name": "...", "category": "...", "unit": "..."}],
  "boms": [{
    "code": "...",
    "product_sku": "...",
    "version": 1,
    "output_quantity": "1.000",
    "status": "active",
    "components": [{"item_sku": "...", "quantity": "1.000", "scrap_percent": "0.00"}]
  }]
}
```

قیمت‌ها عدد صحیح بر حسب تومان‌اند. مقدار مصرف حداکثر سه رقم اعشار و درصد ضایعات حداکثر دو رقم اعشار دارد. `SKU` و کد BOM شناسه‌های پایدار تبادل هستند؛ شناسهٔ داخلی دیتابیس وارد Git نمی‌شود.

## export قطعی

برای تولید دوبارهٔ فایل نمونه از دیتابیس:

```powershell
python manage.py export_product_structure data/demo_product_structure.v1.json `
  --product PKG-201 `
  --include-stock `
  --dataset-code erp-demo-workstation-v1
```

خروجی بر اساس SKU و کد BOM مرتب است، timestamp ندارد و بنابراین diff آن در Git پایدار و قابل بازبینی می‌ماند. بدون `--include-stock` فقط master data صادر می‌شود؛ این حالت برای مخزن دادهٔ مرجع سازمان مناسب‌تر است. گزینهٔ موجودی، ماندهٔ لحظهٔ export را با نام `opening_stock` ثبت می‌کند و برای ساخت محیط تازه کاربرد دارد.

## اعتبارسنجی و import اتمیک

پیش از ادغام pull request یا اعمال روی محیط مقصد:

```powershell
python manage.py import_product_structure data/demo_product_structure.v1.json `
  --dry-run --replace --with-opening-stock
```

`dry-run` تمام عملیات واقعی را داخل تراکنش اجرا و در پایان rollback می‌کند. کنترل‌ها فراتر از JSON Schema هستند:

- یکتایی SKU، کد BOM و نسخهٔ هر محصول؛
- وجود همهٔ ارجاع‌های محصول و جزء؛
- یک BOM فعال برای هر محصول؛
- منع جزء تکراری، ترتیب تکراری و self-reference؛
- کشف حلقه در گراف BOM فعال؛
- کنترل دامنهٔ مقدار خروجی، مصرف و ضایعات؛
- جلوگیری از تغییر معنای یک کد BOM موجود؛
- import تکرارپذیر و بدون ایجاد رکورد تکراری.

اعمال نهایی:

```powershell
python manage.py import_product_structure data/demo_product_structure.v1.json --replace
```

`--replace` اجزایی را که از نسخهٔ فایل حذف شده‌اند از همان BOM حذف می‌کند. `--with-opening-stock` فقط برای کالای بدون مانده و بدون سابقهٔ گردش موجودی ثبت می‌کند؛ موجودی عملیاتی موجود هرگز بازنویسی نمی‌شود.

## گردش پیشنهادی Git

1. شاخهٔ تغییر master data ساخته شود.
2. فایل JSON و در صورت تغییر قرارداد، schema در همان commit اصلاح شوند.
3. `import_product_structure --dry-run --replace` در CI اجرا شود.
4. مسئول مهندسی محصول مقدار مصرف، ضایعات و نسخه را review کند.
5. پس از merge، همان commit در محیط مقصد import شود و شناسهٔ commit در گزارش استقرار ثبت گردد.

برای تولید واقعی، واحد اندازه‌گیری، روش گردکردن، ضایعات، نسخهٔ مهندسی و تاریخ اثربخشی باید با رویهٔ مصوب شرکت تطبیق داده شوند. این قرارداد عمداً داده‌های تراکنشی مانند سفارش، مصرف واقعی و اسناد مالی را وارد Git نمی‌کند.
