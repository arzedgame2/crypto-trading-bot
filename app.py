import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎉 بات با موفقیت راه‌اندازی شد! سلام!")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("راهنما: /start")

def main():
    if not BOT_TOKEN:
        print("❌ توکن بات تنظیم نشده!")
        return
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    
    print("✅ بات فعال شد!")
    application.run_polling()

if __name__ == "__main__":
    main()
