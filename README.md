# OSINT Bot — 24/7 Deploy Guide

## Fixes applied
- ✅ Session path bug: `.user_session` ئێستا لەتەنیشت bot.py پاشەکەوت دەکرێت (پێشتر لە `bot/.user_session` بوو کە لە سێرڤەر نەدۆزرایەوە).
- ✅ TeleSINT handler چارەسەرکرا: لۆگ زیادکرا و یوزەرنەیم case-insensitive match دەکات.
- ✅ Dockerfile + Procfile + railway.json بۆ deploy ی خۆکار.

## Deploy لە Railway (پێشنیارکراو — خۆڕایی، 24/7)

1. بڕۆ [railway.app](https://railway.app) و sign up بکە بە GitHub.
2. ئەم فۆڵدەرە بکە GitHub repo (یان `Deploy from local` بەکاربێنە).
3. `New Project` → `Deploy from GitHub repo` → repo-ەکەت هەڵبژێرە.
4. لە `Variables` ئەمانە زیاد بکە:
   - `BOT_TOKEN` = توکنی بۆتەکەت لە @BotFather
   - `API_ID` = لە my.telegram.org
   - `API_HASH` = لە my.telegram.org
   - `ADMIN_CHAT_ID` = ئایدی تێلیگرامی خۆت
5. Deploy بکە. کاتێک تەواو بوو، لە بۆتەکەت `/genauth` بنووسە و ژمارەکەت بنووسە بۆ login-کردنی userbot.

## Deploy لە Render
- `New +` → `Background Worker` → repo هەڵبژێرە → Dockerfile خۆکار دەناسرێتەوە → env vars زیاد بکە.

## Deploy لە VPS (Ubuntu)
```bash
git clone <your-repo> && cd osint_bot
docker build -t osintbot .
docker run -d --restart=always --name osintbot \
  -e BOT_TOKEN=xxx -e API_ID=xxx -e API_HASH=xxx -e ADMIN_CHAT_ID=xxx \
  -v $(pwd)/data:/app osintbot
```
