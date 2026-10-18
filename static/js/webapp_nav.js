// static/js/webapp_nav.js
//
// Общий слой для всех страниц приложения: связь с Telegram, запросы
// к серверу с пропуском, и всё, что делает приложение «родным» для
// Telegram — цвет шапки, нижняя кнопка, вибрация, кнопка «Назад».
//
// Всё, что зависит от Telegram, проверяет версию клиента: старый
// Telegram, который чего-то не умеет, просто остаётся без этой возможности.
(function () {

  // Цвета приложения — те же, что в app.css
  const THEME = {
    bg: "#0C0E13",
    gold: "#D9B872",
    goldInk: "#14161B",
    doneBg: "#18231C",
    doneText: "#5FB37A",
    quietBg: "#1C2029",
    quietText: "#E6E7EA"
  };

  function tgApp() {
    return (window.Telegram && window.Telegram.WebApp) || null;
  }

  // Умеет ли клиент Telegram возможность из такой-то версии
  function can(version) {
    const tg = tgApp();
    try {
      return !!(tg && tg.isVersionAtLeast && tg.isVersionAtLeast(version));
    } catch (e) {
      return false;
    }
  }

  function getInitData() {
    const tg = tgApp();
    if (tg) {
      try {
        tg.ready();
      } catch (e) {}
      return tg.initData || "";
    }
    return "";
  }

  function plural(n, one, few, many) {
    const last = n % 10;
    const twoLast = n % 100;
    if (twoLast >= 11 && twoLast <= 14) return many;
    if (last === 1) return one;
    if (last >= 2 && last <= 4) return few;
    return many;
  }

  // Что считается «нажатием» для вибрации
  const TAP_TARGETS = ".btn, .icon-btn, .icon-ghost, .favorite-btn, .list-action, .card--link, .disclosure, .composer__send, .stat-tile, .row-link";
  const SELECT_TARGETS = ".tab, .switch, .filter-chip";

  // Адрес страницы, с которой пришли, если это наше же приложение
  function sameOriginReferrer() {
    try {
      if (!document.referrer) return "";
      const from = new URL(document.referrer);
      if (from.origin !== location.origin) return "";
      if (from.pathname === location.pathname) return "";
      return from.pathname + from.search;
    } catch (e) {
      return "";
    }
  }

  // Плавные переходы между страницами (View Transitions).
  // Браузер сам перетекает от старой страницы к новой; мы только говорим,
  // в какую сторону: вперёд — сдвиг влево, назад — вправо.
  // Где браузер этого не умеет, страница открывается сразу, как раньше.
  window.addEventListener("pagereveal", (event) => {
    if (!event.viewTransition) return;

    document.documentElement.classList.add("vt");

    let direction = "forward";
    try {
      direction = sessionStorage.getItem("ow-nav-dir") || "forward";
      sessionStorage.removeItem("ow-nav-dir");
    } catch (e) {}

    try { event.viewTransition.types.add(direction); } catch (e) {}
  });

  window.OsWork = {

    init() {
      const tg = tgApp();
      if (!tg) return;

      try {
        tg.ready();
        tg.expand();
      } catch (e) {}

      // Вне Telegram (обычный браузер) дальше делать нечего
      if (!this.isInsideTelegram()) return;

      // Метка для стилей: внутри Telegram свою кнопку «Назад» на странице прячем
      document.documentElement.classList.add("in-telegram");

      // Шапка и низ Telegram в цвет приложения: исчезает шов между
      // окном Telegram и экраном, приложение выглядит цельным
      try { if (can("6.1")) tg.setBackgroundColor(THEME.bg); } catch (e) {}
      try { if (can("6.9")) tg.setHeaderColor(THEME.bg); } catch (e) {}
      try { if (can("7.10")) tg.setBottomBarColor(THEME.bg); } catch (e) {}

      // Telegram помнит нижние кнопки и «Назад» между страницами.
      // Сбрасываем, чтобы кнопка с прошлой страницы не висела на этой.
      try { tg.MainButton.hideProgress(); tg.MainButton.hide(); } catch (e) {}
      try { if (can("7.10") && tg.SecondaryButton) tg.SecondaryButton.hide(); } catch (e) {}
      try { if (can("6.1")) tg.BackButton.hide(); } catch (e) {}
      try { if (can("6.2")) tg.disableClosingConfirmation(); } catch (e) {}

      // Лёгкая вибрация на любое нажатие по всему приложению
      document.addEventListener("click", (event) => {
        const target = event.target;
        if (!target || !target.closest) return;

        const select = target.closest(SELECT_TARGETS);
        if (select) {
          this.haptic("select");
          return;
        }

        const tap = target.closest(TAP_TARGETS);
        if (tap && !tap.disabled) this.haptic("light");
      }, true);
    },

    /* Родная кнопка «Назад» в шапке Telegram.
       Раньше она показывалась, но ей не назначалось действие —
       стрелка была, нажатие ничего не делало. */
    setBackButton(opts = {}) {
      const tg = tgApp();
      if (!tg || !this.isInsideTelegram() || !can("6.1")) return;

      try {
        if (this._backHandler) {
          tg.BackButton.offClick(this._backHandler);
          this._backHandler = null;
        }

        if (opts.visible === false) {
          tg.BackButton.hide();
          return;
        }

        let target = opts.fallbackUrl || "/";

        // Страницы, куда приходят из разных мест (вакансия, переписка),
        // возвращают туда, откуда пришли, а не в одно фиксированное место
        if (opts.preferReferrer) {
          const from = sameOriginReferrer();
          if (from) target = from;
        }

        this._backHandler = opts.onClick || (() => this.go(target, { replace: true, back: true }));

        tg.BackButton.onClick(this._backHandler);
        tg.BackButton.show();
      } catch (e) {}
    },

    /* Вибрация.
       light / medium / heavy — толчок на нажатие;
       success / error / warning — итог действия;
       select — переключение (вкладки, переключатели). */
    haptic(kind = "light") {
      const tg = tgApp();
      if (!tg || !this.isInsideTelegram() || !can("6.1") || !tg.HapticFeedback) return;

      try {
        if (kind === "success" || kind === "error" || kind === "warning") {
          tg.HapticFeedback.notificationOccurred(kind);
        } else if (kind === "select") {
          tg.HapticFeedback.selectionChanged();
        } else {
          tg.HapticFeedback.impactOccurred(kind);
        }
      } catch (e) {}
    },

    /* Есть ли у клиента родная нижняя кнопка. Если есть — страница
       прячет свою кнопку, чтобы не было двух одинаковых. */
    hasMainButton() {
      const tg = tgApp();
      return !!(tg && this.isInsideTelegram() && tg.MainButton);
    },

    /* Большая родная кнопка внизу экрана Telegram.
       Возвращает пульт управления ею либо null, если кнопки нет. */
    mainButton({ text, onClick, shine = false } = {}) {
      const tg = tgApp();
      if (!this.hasMainButton()) return null;

      const mb = tg.MainButton;

      if (this._mainHandler) {
        try { mb.offClick(this._mainHandler); } catch (e) {}
      }

      // Пока идёт отправка или кнопка погашена — повторные нажатия глушим
      this._mainHandler = () => {
        if (mb.isProgressVisible || !mb.isActive) return;
        if (onClick) onClick();
      };

      const params = {
        text: text,
        color: THEME.gold,
        text_color: THEME.goldInk,
        is_active: true,
        is_visible: true
      };

      // Переливающийся блик на кнопке — только у главных действий
      if (can("7.10")) params.has_shine_effect = !!shine;

      try {
        mb.setParams(params);
        mb.onClick(this._mainHandler);
      } catch (e) {
        return null;
      }

      return {
        setText(value) {
          try { mb.setText(value); } catch (e) {}
        },
        busy(on) {
          try { on ? mb.showProgress(false) : mb.hideProgress(); } catch (e) {}
        },
        // Действие выполнено: кнопка гаснет в зелёный и больше не нажимается
        done(value) {
          try {
            mb.hideProgress();
            const doneParams = {
              text: value,
              color: THEME.doneBg,
              text_color: THEME.doneText,
              is_active: false
            };
            if (can("7.10")) doneParams.has_shine_effect = false;
            mb.setParams(doneParams);
          } catch (e) {}
        },
        hide() {
          try { mb.hideProgress(); mb.hide(); } catch (e) {}
        }
      };
    },

    /* Вторая родная кнопка внизу, рядом с главной (Telegram 7.10+). */
    secondaryButton({ text, onClick, position = "left" } = {}) {
      const tg = tgApp();
      if (!tg || !this.isInsideTelegram() || !can("7.10") || !tg.SecondaryButton) return null;

      const sb = tg.SecondaryButton;

      if (this._secondHandler) {
        try { sb.offClick(this._secondHandler); } catch (e) {}
      }

      this._secondHandler = () => { if (onClick) onClick(); };

      try {
        sb.setParams({
          text: text,
          color: THEME.quietBg,
          text_color: THEME.quietText,
          position: position,
          is_active: true,
          is_visible: true
        });
        sb.onClick(this._secondHandler);
      } catch (e) {
        return null;
      }

      return {
        hide() {
          try { sb.hide(); } catch (e) {}
        }
      };
    },

    /* Защита от случайного закрытия: если в форме есть несохранённые
       изменения, Telegram спросит, точно ли закрыть. */
    setDirty(on) {
      const tg = tgApp();
      if (!tg || !this.isInsideTelegram() || !can("6.2")) return;
      try {
        on ? tg.enableClosingConfirmation() : tg.disableClosingConfirmation();
      } catch (e) {}
    },

    /* Следит за полями внутри контейнера: при первом изменении включает
       защиту от закрытия и сообщает странице, что форма «грязная». */
    trackChanges(container, onChange) {
      if (!container) return;

      const handler = () => {
        this.setDirty(true);
        if (onChange) onChange();
      };

      container.addEventListener("input", handler);
      container.addEventListener("change", handler);
    },

    /* Мягкое появление элементов один за другим */
    stagger(elements) {
      Array.prototype.forEach.call(elements || [], (el, i) => {
        el.style.setProperty("--i", i);
        el.classList.add("rise");
      });
    },

    go(path, options = {}) {
      // Направление запоминаем для следующей страницы: от него зависит,
      // в какую сторону поедет анимация перехода
      try { sessionStorage.setItem("ow-nav-dir", options.back ? "back" : "forward"); } catch (e) {}

      if (options.replace) window.location.replace(path);
      else window.location.href = path;
    },

    safeBack(fallbackUrl = "/") {
      if (window.history.length > 1) window.history.back();
      else window.location.href = fallbackUrl;
    },

    apiFetch(url, options = {}) {
      options.headers = options.headers || {};

      const initData = getInitData();

      if (initData) {
        options.headers["X-Telegram-Init-Data"] = initData;
      }

      if (!options.headers["Content-Type"] && options.method === "POST") {
        options.headers["Content-Type"] = "application/json";
      }

      options.cache = "no-store";

      return fetch(url, options);
    },

    /* Разделение телефона по мере ввода.
       Человек набирает подряд, а в поле появляется +7 (918) 123-45-67.
       Сервер всё равно проверяет только цифры, так что разделители
       нужны исключительно для читаемости. */
    formatPhone(raw) {
      let digits = String(raw || "").replace(/\D/g, "");

      if (!digits) return "";

      // 8 в начале — привычная запись того же российского кода
      if (digits[0] === "8") digits = "7" + digits.slice(1);

      // человек начал сразу с девятки — подставляем код страны
      if (digits[0] === "9" && digits.length <= 10) digits = "7" + digits;

      // не российский номер: оставляем цифры как есть
      if (digits[0] !== "7") return "+" + digits.slice(0, 15);

      digits = digits.slice(0, 11);

      const rest = digits.slice(1);
      let out = "+7";

      if (rest.length) out += " (" + rest.slice(0, 3);
      if (rest.length >= 3) out += ")";
      if (rest.length > 3) out += " " + rest.slice(3, 6);
      if (rest.length > 6) out += "-" + rest.slice(6, 8);
      if (rest.length > 8) out += "-" + rest.slice(8, 10);

      return out;
    },

    attachPhoneMask(input) {
      if (!input) return;

      let previous = "";

      const apply = (event) => {
        const caretAtEnd = input.selectionStart === input.value.length;
        const deleting = event && String(event.inputType || "").startsWith("delete");

        let formatted = this.formatPhone(input.value);

        // Стирание разделителя: цифры не изменились, значит для человека
        // ничего не произошло. Убираем последнюю цифру, иначе клавиша
        // стирания выглядит сломанной.
        if (deleting && formatted === previous) {
          formatted = this.formatPhone(input.value.replace(/\D/g, "").slice(0, -1));
        }

        if (formatted !== input.value) {
          input.value = formatted;

          if (caretAtEnd) {
            try {
              input.setSelectionRange(formatted.length, formatted.length);
            } catch (e) {}
          }
        }

        previous = formatted;
      };

      input.addEventListener("input", apply);
      apply(null);
    },

    /* Пересылка вакансии.
       Открывает окно выбора чата прямо в Telegram: человек выбирает,
       кому отправить, и ссылка уходит с коротким описанием.
       По ссылке вакансия открывается даже у того, кто бота не запускал. */
    shareJob(jobId, title) {
      const username = window.BOT_USERNAME || "";
      if (!username) return;

      const link = "https://t.me/" + username + "?start=job_" + Number(jobId);
      const text = title || "Вакансия";

      const tg = tgApp();

      if (tg && tg.openTelegramLink) {
        tg.openTelegramLink(
          "https://t.me/share/url?url=" + encodeURIComponent(link) +
          "&text=" + encodeURIComponent(text)
        );
        return;
      }

      // Вне Telegram выбирать чат не у кого — просто отдаём ссылку
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(link).then(
          function () { alert("Ссылка скопирована:\n\n" + link); },
          function () { alert(link); }
        );
      } else {
        alert(link);
      }
    },

    /* Открыто ли приложение внутри Telegram.
       Вне Telegram пропуска нет и быть не может: его выдаёт сам клиент. */
    isInsideTelegram() {
      const tg = tgApp();
      return !!(tg && tg.initData && tg.initData.length > 0);
    },

    botLink() {
      const username = window.BOT_USERNAME || "";
      return username ? "https://t.me/" + username : "";
    },

    /* Страницы, где без пропуска делать нечего: кабинеты, формы, чаты.
       Возвращает true, если заглушка показана — вызывающий код должен
       остановиться, иначе он полезет к элементам, которых больше нет. */
    blockOutsideTelegram() {
      if (this.isInsideTelegram()) return false;

      const link = this.botLink();

      document.body.innerHTML =
        '<div class="stub">' +
          '<div class="stub__icon">' +
            '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' +
            '<path d="M21 4 3 11l6 2.5L19 7l-7.5 8.5.5 5 3-3.5 4 3z"/></svg>' +
          '</div>' +
          '<h1 class="stub__title">Откройте в Telegram</h1>' +
          '<p class="stub__text">Эта страница работает внутри Telegram: оттуда приложение знает, кто вы, ' +
          'и может показать ваше резюме, отклики и переписку. В обычном браузере этого нет.</p>' +
          (link ? '<a class="btn btn--primary" href="' + link + '">Открыть бота</a>' : '') +
        '</div>';

      return true;
    },

    /* Страницы, которые и в браузере полезны: список вакансий и сама
       вакансия. Их не прячем — показываем полосу сверху. */
    noticeOutsideTelegram() {
      if (this.isInsideTelegram()) return false;

      const link = this.botLink();

      const bar = document.createElement("div");
      bar.className = "tg-notice";
      bar.innerHTML =
        '<span>Смотреть можно здесь. Чтобы откликнуться — откройте в Telegram.</span>' +
        (link ? '<a class="btn btn--quiet btn--sm" href="' + link + '">Открыть</a>' : '');

      document.body.insertBefore(bar, document.body.firstChild);
      return true;
    },

    /* «2 дня назад» вместо голой даты.

       База отдаёт время по Гринвичу и без пометки об этом:
       «2026-09-18 18:00:51». Браузер по умолчанию считает такую строку
       местным временем и ошибается на несколько часов — поэтому
       пометку добавляем сами. */
    timeAgo(value) {
      if (!value) return "";

      const raw = String(value).trim().replace(" ", "T");
      const hasZone = /[zZ]$|[+-]\d\d:?\d\d$/.test(raw);
      const date = new Date(hasZone ? raw : raw + "Z");

      if (isNaN(date.getTime())) return "";

      const seconds = Math.floor((Date.now() - date.getTime()) / 1000);

      if (seconds < 0) return "";
      if (seconds < 90) return "только что";

      const minutes = Math.floor(seconds / 60);
      if (minutes < 60) return minutes + " " + plural(minutes, "минуту", "минуты", "минут") + " назад";

      const hours = Math.floor(minutes / 60);
      if (hours < 24) return hours + " " + plural(hours, "час", "часа", "часов") + " назад";

      const days = Math.floor(hours / 24);
      if (days === 1) return "вчера";
      if (days < 7) return days + " " + plural(days, "день", "дня", "дней") + " назад";

      const weeks = Math.floor(days / 7);
      if (weeks < 5) return weeks + " " + plural(weeks, "неделю", "недели", "недель") + " назад";

      const months = Math.floor(days / 30);
      if (months < 12) return months + " " + plural(months, "месяц", "месяца", "месяцев") + " назад";

      return "больше года назад";
    }

  };

})();
