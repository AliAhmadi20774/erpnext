"""Page explanations and role-appropriate presentation routes."""
from django.urls import reverse

from .access import ROLE_FINANCE, ROLE_INVENTORY, ROLE_MANAGER, ROLE_PRODUCTION, ROLE_PURCHASE, ROLE_SALES, user_roles
from .models import Order, ProductionPlan


def step(target, title, body):
    return {"target": target, "title": title, "body": body}


GUIDES = {
    "workspace": [
        step(".workspace-hero", "از اینجا شروع کنید", "این صفحه میز کار نقش شماست. تعداد اقدام‌های باز از اسناد جاری محاسبه می‌شود؛ ابتدا ببینید امروز چه چیزی منتظر رسیدگی است."),
        step(".module-grid", "فرایند را انتخاب کنید", "هر کارت یک مسیر کاری دارد: ثبت سفارش، تامین، تولید یا امور مالی. لینک‌های داخل کارت به سند و اقدام مربوط می‌رسند."),
        step(".task-panel", "اقدام بعدی را پیدا کنید", "صف اقدام، سند و مسئول رسیدگی را مشخص می‌کند. یک مورد را باز کنید و از همان مسئله کار را ادامه دهید."),
    ],
    "dashboard": [
        step(".presentation-cards", "سناریوی آماده را باز کنید", "این کارت‌ها نقطهٔ ورود ارائه‌اند: سفارش دارای کمبود، سفارش تسویه‌شده، ماندهٔ وصول و کنترل کیفیت. شمارهٔ هر کارت به سند همین محیط وصل است."),
        step(".stats-grid:not(.presentation-cards)", "فروش با وصول فرق دارد", "فروش تاییدشده مبلغ سفارش است؛ ماندهٔ دریافتنی از صورتحساب و پرداخت محاسبه می‌شود. برای توضیح هر عدد روی کارت گزارش آن کلیک کنید."),
        step(".chart-panel", "بازهٔ گزارش را بخوانید", "نمودار شش ماه شمسی را نشان می‌دهد. فروش تاییدشده به معنی تحویل کامل یا دریافت وجه نیست؛ هر ستون به سفارش‌های همان ماه راه دارد."),
    ],
    "product_tree": [
        step(".product-toolbar", "محصول و تعداد را تعیین کنید", "محصول نهایی یا زیرمونتاژ را انتخاب و «نمایش» را بزنید. تعداد برنامهٔ تولید را تغییر دهید و «محاسبه» را بزنید تا نیاز همان مقدار محاسبه شود."),
        step(".tree-stats", "ساختار را در یک نگاه بخوانید", "قطعهٔ مشترک در چند شاخه تکرار می‌شود؛ تعداد اجزای منفجرشده با تعداد کدهای کالا فرق دارد. بهای این صفحه فقط مواد است؛ هزینهٔ ساخت با دستمزد و سربار در برآورد سفارش دیده می‌شود."),
        step(".product-tree-panel .panel-header", "از محصول تا قطعه پایین بروید", "شاخه‌ها یا «باز کردن همه» را بزنید. نیاز با خروجی BOM و ضایعات محاسبه می‌شود. کمبود هر ردیف با موجودی همان قلم مقایسه می‌شود؛ تخصیص قطعات مشترک را در MRP ببینید."),
    ],
    "order_detail": [
        step(".details", "تعهد مشتری را بخوانید", "طرف حساب، موعد و یادداشت سفارش مبنای کار هستند. تایید سفارش تعهد را ثبت می‌کند؛ تحویل، صورتحساب و تسویه مراحل جداگانه‌اند."),
        step(".table-panel", "اقلام و باقیمانده را بررسی کنید", "تعداد سفارش، انجام‌شده و باقیمانده را مقایسه کنید. صورتحساب فقط تحویل ثبت‌شده را پوشش می‌دهد؛ قیمت سفارش سابقهٔ همان سند است."),
        step("[aria-label='مسیر یکپارچهٔ سفارش مشتری']", "مانع و سند مرتبط را دنبال کنید", "این مسیر سفارش، برنامهٔ مواد، خرید و ساخت را به هم وصل می‌کند. «اقدام بعدی» نشان می‌دهد کار دست کدام نقش است و چه چیزی هنوز باید انجام شود."),
        step(".summary-panel", "اقدام را آگاهانه انجام دهید", "دکمه‌های این بخش عملیات واقعی همین محیط را ثبت می‌کنند. پیش از تحویل موجودی و پیش از وصول ماندهٔ صورتحساب را بخوانید. راهنما عملیاتی را خودکار اجرا نمی‌کند."),
    ],
    "production_plan_detail": [
        step(".page-heading", "تقاضا و موعد مبنا", "این برنامه نیاز مواد یک محصول را برای مقدار و موعد مشخص ذخیره کرده است. پیوند سفارش مشتری تقاضای مبنا را نشان می‌دهد."),
        step(".stats-grid", "ساخت را از خرید جدا کنید", "زیرمونتاژ دارای BOM پیشنهاد ساخت می‌گیرد و قطعهٔ خریدنی پیشنهاد خرید. تبدیل پیشنهاد، سفارش اجرایی ایجاد می‌کند."),
        step(".table-panel", "نیاز خالص را توضیح دهید", "نیاز ناخالص منهای موجودی تخصیص‌یافته و دریافت بازِ به‌موقع، نیاز خالص است. قطعات مشترک از موجودی مشترک یک‌بار پوشش می‌گیرند. جدول مبنای ذخیره‌شده است؛ ظرفیت ماشین و رزرو اختصاصی بررسی نمی‌شوند."),
    ],
    "purchase_approvals": [
        step(".page-heading", "اختیار خرید را کنترل کنید", "خرید بالاتر از سقف مجاز به تصمیم مدیر نیاز دارد. بررسی کنید تامین برای کدام کمبود و چه زمانی درخواست شده است."),
        step(".panel.spaced", "دلیل و مبلغ را بخوانید", "سند خرید را برای اقلام، مبلغ و دلیل درخواست باز کنید. تایید مدیر با تایید عملیاتی و دریافت کالا فرق دارد؛ تغییر اقلام تایید قبلی را بی‌اعتبار می‌کند."),
    ],
    "inventory": [
        step(".mini-stats", "موجودی را با واحد بخوانید", "تعداد انواع کالا با مجموع واحدها فرق دارد. جمع متر و عدد معیار ظرفیت تولید نیست؛ نیاز هر قلم را جدا بررسی کنید."),
        step(".table-panel", "دفتر گردش هر قلم را باز کنید", "روی نام کالا کلیک کنید. موجودی باید با آخرین ماندهٔ گردش برابر باشد. حد سفارش هشدار تامین است و با نیاز خالص برنامهٔ تولید تفاوت دارد."),
        step(".table-panel.spaced", "اثر عملیات را دنبال کنید", "دریافت خرید موجودی را زیاد و تحویل یا مصرف تولید آن را کم می‌کند. گردش زمان، سند و ماندهٔ بعد را نگه می‌دارد."),
    ],
    "accounting": [
        step(".stats-grid", "اثر مالی عملیات", "اعداد از ثبت‌های دوبل به دست می‌آیند. بانک، دریافتنی، پرداختنی و موجودی مفهوم‌های متفاوت‌اند؛ مبلغ فروش را به‌جای وجه دریافت‌شده نخوانید."),
        step(".table-panel", "توازن را کنترل کنید", "جمع بدهکار و بستانکار باید برابر باشد. برای فهم مبلغ سند مبنای ثبت را هم بررسی کنید."),
        step(".table-panel:last-child", "از دفتر به سند برگردید", "شمارهٔ روزنامه جزئیات ثبت و سند مبنا عملیات مربوط را باز می‌کند. دفاتر قانونی و سیاست‌های مالی شرکت باید در پایلوت تعیین شوند."),
    ],
    "reports": [
        step(".filter-bar", "نوع و دورهٔ گزارش", "گزارش فروش، ماندهٔ وصول و گردش انبار به پرسش‌های متفاوت پاسخ می‌دهند. ابتدا نوع و بازه را انتخاب کنید."),
        step(".table-scroll", "عدد را تا سند پیگیری کنید", "ردیف‌ها به سوابق مرتبط راه دارند. پیش از مقایسهٔ جمع‌ها دوره و تعریف شاخص را یکسان کنید. CSV نتیجهٔ فیلترشده را صادر می‌کند."),
    ],
    "audit_events": [
        step(".page-heading", "چه کسی، چه کاری، چه زمانی؟", "دفتر ممیزی ردپای عملیات حساس را نگه می‌دارد و از این صفحه ویرایش نمی‌شود. رویدادهای خودکار با کاربر «سیستم» نمایش داده می‌شوند."),
        step(".table-panel", "رویداد را با سند تطبیق دهید", "زمان، کاربر، رویداد و سند را کنار هم بخوانید. جزئیات مقادیر عملیات را ثبت می‌کند؛ ممیزی با سند مالی متفاوت است."),
    ],
    "product_scope": [
        step(".page-heading", "جمع‌بندی ارائه", "مشخص کنید کدام فرایند را دیده‌اید و کدام نیاز باید با دادهٔ واقعی شرکت بررسی شود. هدف جلسه انتخاب دامنه و مسئول پایلوت است."),
        step(".panel", "گام بعدی را مشخص کنید", "برای نیاز واقعی مالک، شاهد، معیار پذیرش و موعد تعیین کنید. نتیجهٔ واقعی جلسه را در بخش تصمیم پس از ارائه ثبت کنید."),
    ],
    "scenario_detail": [
        step(".panel", "فرض‌های تصمیم را بخوانید", "سناریو تغییر تقاضا، زمان تامین یا قیمت را با مبنا مقایسه می‌کند. شبیه‌سازی موجودی و اسناد عملیاتی را تغییر نمی‌دهد."),
        step(".table-panel", "اختلاف را تفسیر کنید", "نیاز، هزینه و آمادگی برآوردی را مقایسه کنید. تبدیل به برنامهٔ مستقل یک اقدام جداست و سفارش مشتری را تغییر نمی‌دهد."),
    ],
}

LIST_GUIDE = [step(".page-heading", "هدف این فهرست", "نام، وضعیت و موعد رکوردها را بخوانید؛ پیوند هر ردیف جزئیات سند یا دادهٔ پایه را باز می‌کند."),
              step(".filter-bar", "نتیجه را محدود کنید", "جست‌وجو و فیلتر را متناسب با پرسش انتخاب کنید. پیش از مقایسهٔ جمع‌ها فیلتر و دوره را بررسی کنید."),
              step(".table-panel, .panel", "از فهرست به اقدام", "شمارهٔ سند یا نام رکورد را باز کنید. پیش‌نویس، تایید، انجام و تسویه مراحل متفاوت‌اند؛ جزئیات مشخص می‌کند چه اقدامی باقی مانده است.")]
FORM_GUIDE = [step(".page-heading", "پیش از ثبت", "هدف، سند مبنا و واحد مقادیر را بخوانید. بستن راهنما هیچ داده‌ای ثبت نمی‌کند."),
              step("#main-content form", "ورودی و نتیجه", "فیلدهای الزامی را بررسی کنید. فقط دکمهٔ ثبت خود فرم تغییر را اعمال می‌کند؛ پیام نتیجه یا خطا بعد از ارسال نمایش داده می‌شود.")]
for name in ("orders", "customers", "suppliers", "items", "bom_versions", "work_orders", "production_plans", "exceptions", "management_decisions", "purchase_recommendations"):
    GUIDES.setdefault(name, LIST_GUIDE)
for name in ("order_new", "order_edit", "bom_new", "bom_edit", "work_order_new", "production_plan_new", "item_new", "item_edit", "customer_new", "supplier_new", "stock_adjust", "order_payment", "order_partial", "scenario_new", "management_decision_new", "management_decision_edit", "fit_gap_new", "fit_gap_edit", "order_dates", "purchase_policy", "planning_policy"):
    GUIDES.setdefault(name, FORM_GUIDE)


def guidance_context(request):
    if not request.user.is_authenticated or not request.resolver_match:
        return {}
    roles = user_roles(request.user)
    if not roles:
        return {}
    name = request.resolver_match.url_name
    journey = []

    def add(page, url, allowed):
        if ROLE_MANAGER in roles or roles.intersection(allowed):
            journey.extend({**entry, "url": url} for entry in GUIDES[page])

    add("workspace", reverse("demo:workspace"), roles)
    add("dashboard", reverse("demo:dashboard"), {ROLE_MANAGER})
    order = Order.objects.filter(notes="DEMO-INDUSTRIAL-JOURNEY").first() or Order.objects.filter(notes="DEMO-CUSTOMER-JOURNEY").first()
    line = order.lines.first() if order else None
    tree_url = reverse("demo:product_tree_detail", args=[line.item_id]) if line else reverse("demo:product_tree")
    add("product_tree", tree_url, roles)
    if order:
        add("order_detail", reverse("demo:order_detail", args=[order.pk]), {ROLE_SALES, ROLE_INVENTORY, ROLE_FINANCE})
        plan = ProductionPlan.objects.filter(source_order_line__order=order).order_by("pk").first()
        if plan:
            add("production_plan_detail", reverse("demo:production_plan_detail", args=[plan.pk]), {ROLE_PRODUCTION, ROLE_PURCHASE})
    for page, allowed in (("purchase_approvals", {ROLE_MANAGER}), ("inventory", {ROLE_INVENTORY}), ("accounting", {ROLE_FINANCE}), ("reports", {ROLE_FINANCE}), ("audit_events", {ROLE_MANAGER}), ("product_scope", roles)):
        add(page, reverse(f"demo:{page}"), allowed)
    page = "product_tree" if name == "product_tree_detail" else name
    instructions = GUIDES.get(page, [step(".page-heading, #main-content h1", "موضوع این صفحه", "عنوان، سند مبنا و وضعیت را بررسی کنید؛ این صفحه جزئیات فرایند یا رکورد انتخاب‌شده را نشان می‌دهد."),
                                    step("#main-content .panel", "جزئیات و مسیر ادامه", "پیوندها سوابق مرتبط را باز می‌کنند. دکمه‌های ثبت و تایید، عملیات همین محیط را انجام می‌دهند؛ ابتدا مقادیر و نتیجهٔ مورد انتظار را بررسی کنید.")])
    return {"demo_guide": {"page": instructions, "journey": journey,
                           "storage_key": f"demo-guide:v1:user-{request.user.pk}",
                           "home": name in ("workspace", "dashboard")}}
