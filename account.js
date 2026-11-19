// Shared per-device account (personal token) handling for all Pick3 pages.
//
// A token arrives once via a personal link (#t=<token> or ?token=<token>) and is kept in
// localStorage. If the device already holds a DIFFERENT token (e.g. an admin tapped a link
// they were sending to someone else), we ask before switching accounts.
(function () {
  const KEY = "pick3_token";
  const NAME_KEY = "pick3_name";
  const API = location.hostname.endsWith("github.io") || location.protocol === "file:"
    ? "https://pick3-api.onrender.com" : "";

  const read = (k) => { try { return localStorage.getItem(k) || ""; } catch { return ""; } };
  const write = (k, v) => { try { v ? localStorage.setItem(k, v) : localStorage.removeItem(k); } catch {} };

  async function lookupName(token, timeoutMs) {
    if (!token) return "";
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs || 6000);
    try {
      const res = await fetch(`${API}/api/me`, { headers: { "X-Pick3-Token": token }, signal: ctrl.signal });
      if (!res.ok) return res.status === 401 ? null : "";  // null = token rejected
      return (await res.json()).name || "";
    } catch {
      return "";  // offline / server waking up: unknown, not invalid
    } finally {
      clearTimeout(timer);
    }
  }

  function tokenFromUrl() {
    const hash = new URLSearchParams(location.hash.slice(1));
    const query = new URLSearchParams(location.search);
    const t = (hash.get("t") || query.get("token") || "").trim();
    if (t) {
      query.delete("token");
      const qs = query.toString();
      history.replaceState(null, "", location.pathname + (qs ? `?${qs}` : ""));
    }
    return t;
  }

  // Accepts a bare token or a whole personal link.
  function parseToken(text) {
    const s = String(text || "").trim();
    const m = s.match(/[#&?](?:t|token)=([^&#\s]+)/);
    return m ? decodeURIComponent(m[1]) : s;
  }

  async function adopt(newToken, { ask } = { ask: true }) {
    const current = read(KEY);
    if (!newToken || newToken === current) return { token: current, switched: false };
    if (current && ask) {
      const [curName, newName] = await Promise.all([
        read(NAME_KEY) || lookupName(current, 5000), lookupName(newToken, 5000),
      ]);
      const from = curName ? `${curName} 님` : "다른 계정";
      const to = newName ? `${newName} 님` : "링크의 계정";
      if (!confirm(`이 기기는 지금 ${from}(으)로 사용 중입니다.\n${to}(으)로 바꿀까요?\n\n다른 사람에게 보내려던 링크라면 [취소]를 누르세요.`)) {
        return { token: current, switched: false, declined: newToken };
      }
      write(NAME_KEY, newName || "");
    } else {
      write(NAME_KEY, "");
    }
    write(KEY, newToken);
    return { token: newToken, switched: true };
  }

  const linkToken = tokenFromUrl();
  const ready = adopt(linkToken);

  window.Pick3Account = {
    API,
    ready,                      // resolves once a link token (if any) was accepted or declined
    linkToken,                  // token carried by the opened link, even if not adopted
    getToken: () => read(KEY),
    cachedName: () => read(NAME_KEY),
    // Fresh name from the server (falls back to the cached one). null = token no longer valid.
    async name() {
      const token = read(KEY);
      if (!token) return "";
      const n = await lookupName(token, 60000);
      if (n === null) return null;
      if (n) write(NAME_KEY, n);
      return n || read(NAME_KEY);
    },
    // Prompt for a token or personal link, verify it, then switch. Returns the new name or "".
    async switchInteractive() {
      const typed = prompt("바꿀 계정의 개인 링크나 토큰을 붙여넣으세요:", "");
      const token = parseToken(typed);
      if (!token) return "";
      const n = await lookupName(token, 15000);
      if (n === null) { alert("등록되지 않은 토큰입니다."); return ""; }
      write(KEY, token);
      write(NAME_KEY, n || "");
      return n || "새 계정";
    },
    // Does the signed-in user already have a PIN? (true/false, or undefined if unknown)
    async hasPin() {
      const token = read(KEY);
      if (!token) return undefined;
      try {
        const res = await fetch(`${API}/api/me`, { headers: { "X-Pick3-Token": token } });
        return res.ok ? (await res.json()).has_pin : undefined;
      } catch { return undefined; }
    },
    // Create/change the PIN of the signed-in user. Returns "" on success, else an error message.
    async setPin(pin) {
      const res = await fetch(`${API}/api/pin`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Pick3-Token": read(KEY) },
        body: JSON.stringify({ pin }),
      }).catch(() => null);
      if (!res) return "서버에 연결하지 못했습니다. 잠시 후 다시 해 주세요.";
      if (res.ok) return "";
      const d = await res.json().catch(() => ({}));
      return typeof d.detail === "string" ? d.detail : "PIN을 저장하지 못했습니다.";
    },
    // Name + PIN sign-in. Returns "" on success (token stored), else an error message.
    async loginWithPin(name, pin) {
      const res = await fetch(`${API}/api/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, pin }),
      }).catch(() => null);
      if (!res) return "서버에 연결하지 못했습니다. 서버가 깨어나는 중이면 30초 뒤 다시 해 주세요.";
      const d = await res.json().catch(() => ({}));
      if (!res.ok) return typeof d.detail === "string" ? d.detail : "로그인하지 못했습니다.";
      write(KEY, d.token);
      write(NAME_KEY, d.name || "");
      return "";
    },
    signOut() {
      const token = read(KEY);
      if (token.startsWith("p3s_")) {
        fetch(`${API}/api/logout`, { method: "POST", headers: { "X-Pick3-Token": token } }).catch(() => {});
      }
      write(KEY, ""); write(NAME_KEY, "");
    },
    forget401() { write(NAME_KEY, ""); },
  };
})();
