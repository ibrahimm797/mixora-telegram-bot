import os
from urllib.parse import urlencode

async def product_search(update, context, is_owner):
    if not is_owner(update):
        return

    query = " ".join(context.args).strip()
    if not query:
        await update.effective_message.reply_text(
            "اكتب اسم المنتج بعد الأمر.\nمثال:\n/product كاميرا مراقبة Hikvision"
        )
        return

    tracking_id = os.getenv("AMAZON_TRACKING_ID", "").strip()

    params = {"k": query}
    if tracking_id:
        params["tag"] = tracking_id

    url = "https://www.amazon.eg/s?" + urlencode(params)

    message = (
        f"🔎 نتائج البحث عن: {query}\n\n"
        f"🛒 افتح نتائج أمازون مصر:\n{url}\n\n"
        "قد أحصل على عمولة من المشتريات المؤهلة عبر رابط الأفلييت."
    )

    if not tracking_id:
        message += (
            "\n\n⚠️ لم يتم إعداد AMAZON_TRACKING_ID بعد؛ "
            "الرابط الحالي ليس رابط عمولة."
        )

    await update.effective_message.reply_text(
        message,
        disable_web_page_preview=True
    )
