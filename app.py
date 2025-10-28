import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# تنظیم لاگ
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get('BOT_TOKEN')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎉 بات با موفقیت راه‌اندازی شد! سلام!")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("راهنما: /start")

def main():
    if not BOT_TOKEN:
        logger.error("❌ توکن بات یافت نشد! لطفا BOT_TOKEN را تنظیم کنید.")
        return
    
    try:
        application = Application.builder().token(BOT_TOKEN).build()
        
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_cmd))
        
        logger.info("✅ بات در حال راه‌اندازی...")
        application.run_polling()
        
    except Exception as e:
        logger.error(f"❌ خطا در راه‌اندازی بات: {e}")

if __name__ == "__main__":
    main()
