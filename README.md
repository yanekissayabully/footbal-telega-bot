# Football Telegram bot (Бразилия, pt-BR)

Берёт свежие новости из бразильских RSS (ge.globo, Trivela, Gazeta Esportiva, Torcedores, Placar),
находит картинку и постит в канал: заголовок + краткое описание + ссылка.

## Запуск
1. @BotFather → /newbot → токен.
2. Добавить бота в канал админом (право публиковать сообщения).
3. `copy .env.example .env` и заполнить BOT_TOKEN и CHANNEL_ID.
4. `.venv\Scripts\python bot.py` (или `pip install -r requirements.txt` в своём окружении).

Первый запуск публикует только самые свежие новости (MAX_POSTS_PER_RUN), остальное помечает как виденное.
Источники — список FEEDS в bot.py.
