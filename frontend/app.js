"use strict";

// Sign-in: OAuth 2.0 authorization code with PKCE against Cognito managed login.
// The page holds no secret; the code verifier proves that the same browser started and
// finished the sign-in. Tokens live in sessionStorage, so closing the tab signs out.

const SCOPES = "openid email";
const EXPIRY_MARGIN_MS = 30_000;
const store = window.sessionStorage;
const redirectUri = `${window.location.origin}/`;
let config = null;

const $ = (id) => document.getElementById(id);

function base64url(bytes) {
  let binary = "";
  for (const byte of new Uint8Array(bytes)) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomString() {
  return base64url(crypto.getRandomValues(new Uint8Array(32)));
}

async function codeChallenge(verifier) {
  return base64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
}

async function signIn() {
  const verifier = randomString();
  const state = randomString();
  store.setItem("pkce_verifier", verifier);
  store.setItem("pkce_state", state);
  const params = new URLSearchParams({
    response_type: "code",
    client_id: config.clientId,
    redirect_uri: redirectUri,
    scope: SCOPES,
    state,
    code_challenge_method: "S256",
    code_challenge: await codeChallenge(verifier),
  });
  window.location.assign(`${config.loginDomain}/oauth2/authorize?${params}`);
}

async function finishSignIn(params) {
  const verifier = store.getItem("pkce_verifier");
  const expectedState = store.getItem("pkce_state");
  store.removeItem("pkce_verifier");
  store.removeItem("pkce_state");
  window.history.replaceState(null, "", "/");
  if (!verifier || params.get("state") !== expectedState) {
    throw new Error("Sign-in didn't complete because the response didn't match this tab. Sign in again.");
  }
  const response = await fetch(`${config.loginDomain}/oauth2/token`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: config.clientId,
      code: params.get("code"),
      redirect_uri: redirectUri,
      code_verifier: verifier,
    }),
  });
  if (!response.ok) {
    throw new Error(`Sign-in failed: the login service returned ${response.status}. Sign in again.`);
  }
  const tokens = await response.json();
  store.setItem("access_token", tokens.access_token);
  store.setItem("expires_at", String(Date.now() + tokens.expires_in * 1000));
}

function accessToken() {
  const token = store.getItem("access_token");
  const expiresAt = Number(store.getItem("expires_at"));
  return token && Date.now() < expiresAt - EXPIRY_MARGIN_MS ? token : null;
}

function signOut() {
  store.clear();
  const params = new URLSearchParams({ client_id: config.clientId, logout_uri: redirectUri });
  window.location.assign(`${config.loginDomain}/logout?${params}`);
}

// Asking: POST /query with the access token; API Gateway verifies it before the Lambda runs.

function setStatus(text, warn = false) {
  $("status").textContent = text;
  $("status").classList.toggle("warn", warn);
}

function el(tag, props = {}, children = []) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

function openSource(number) {
  const details = document.querySelector(`#source-${number} details`);
  if (!details) return;
  details.open = true;
  details.scrollIntoView({ block: "nearest", behavior: "smooth" });
  details.querySelector("summary").focus({ preventScroll: true });
}

// Hovering or focusing a marker highlights its source, and the other way round.
function link(number, on) {
  document.getElementById(`source-${number}`)?.classList.toggle("linked", on);
  for (const marker of document.querySelectorAll(`.marker[data-n="${number}"]`)) {
    marker.classList.toggle("linked", on);
  }
}

function linkOnHover(node, number) {
  for (const [type, on] of [["mouseenter", true], ["mouseleave", false], ["focusin", true], ["focusout", false]]) {
    node.addEventListener(type, () => link(number, on));
  }
}

function sourceItem(citation, passage) {
  const pages = citation.page_start === citation.page_end
    ? `page ${citation.page_start}`
    : `pages ${citation.page_start}-${citation.page_end}`;
  const score = passage ? Math.min(Math.max(passage.score, 0), 1) : 0;
  const fill = el("div", { className: "fill" });
  fill.style.width = `${Math.max(score * 100, 2)}%`;
  const relevance = el("span", { className: "relevance", title: "Reranker relevance score, 0 to 1" }, [
    `relevance ${score.toFixed(2)}`,
    el("span", { className: "bar" }, [fill]),
  ]);
  const summary = el("summary", {}, [
    el("span", { className: "number", textContent: `${citation.number}` }),
    el("span", { className: "where" }, [
      `${citation.doc_id}, ${pages}`,
      el("br"),
      el("a", {
        href: `https://doi.org/${encodeURI(citation.doi)}`,
        target: "_blank",
        rel: "noopener noreferrer",
        textContent: `doi:${citation.doi}`,
      }),
    ]),
    relevance,
  ]);
  const quote = el("blockquote", { textContent: passage ? passage.text : "" });
  const item = el("li", { id: `source-${citation.number}` }, [el("details", {}, [summary, quote])]);
  linkOnHover(summary, citation.number);
  return item;
}

function renderAnswer(result) {
  const answer = $("answer");
  const sources = $("sources");
  answer.replaceChildren();
  sources.replaceChildren();
  answer.classList.toggle("abstained", result.abstained);

  if (result.abstained) {
    answer.append(
      el("strong", { textContent: "No answer" }),
      "The papers don't cover this question, so there is no answer.",
    );
  } else {
    const cited = new Set(result.citations.map((c) => c.number));
    for (const part of result.text.split(/(\[\d+\])/)) {
      const match = part.match(/^\[(\d+)\]$/);
      if (match && cited.has(Number(match[1]))) {
        const number = Number(match[1]);
        const marker = el("button", { type: "button", className: "marker", textContent: `${number}` });
        marker.dataset.n = String(number);
        marker.setAttribute("aria-label", `Show source ${number}`);
        marker.addEventListener("click", () => openSource(number));
        linkOnHover(marker, number);
        answer.append(marker);
      } else {
        answer.append(part);
      }
    }
    for (const citation of result.citations) {
      sources.append(sourceItem(citation, result.passages[citation.number - 1]));
    }
  }

  $("sources-heading").hidden = result.abstained || result.citations.length === 0;
  $("meta").textContent = `Answered by ${result.generation_model_id}, configuration ${result.config_id}.`;
  $("result").hidden = false;
}

async function ask(question) {
  const token = accessToken();
  if (!token) {
    render();
    setStatus("Your session has expired. Sign in again.", true);
    return;
  }
  let response;
  try {
    response = await fetch(new URL("query", config.apiUrl), {
      method: "POST",
      headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
      body: JSON.stringify({ question }),
    });
  } catch {
    setStatus("The request didn't reach the service. Check your connection, or sign in again if this keeps happening.", true);
    return;
  }
  if (response.status === 401) {
    store.removeItem("access_token");
    render();
    setStatus("Your session is no longer valid. Sign in again.", true);
    return;
  }
  if (response.status === 429) {
    setStatus("The service is busy: it answers about two questions per minute. Try again in 30 seconds.", true);
    return;
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    setStatus(body.error ? `The question wasn't answered: ${body.error}.` : `The service returned ${response.status}.`, true);
    return;
  }
  setStatus("");
  renderAnswer(body);
}

// Page state.

function render() {
  const signedIn = accessToken() !== null;
  $("auth").hidden = config === null;
  $("auth").textContent = signedIn ? "Sign out" : "Sign in";
  $("ask").hidden = !signedIn;
  if (!signedIn) $("result").hidden = true;
}

async function init() {
  $("auth").addEventListener("click", () => (accessToken() ? signOut() : signIn()));
  const updateCount = () => {
    $("count").textContent = `${$("question").value.length} / 500, Ctrl+Enter to ask`;
  };
  $("question").addEventListener("input", updateCount);
  $("question").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      $("ask").requestSubmit();
    }
  });
  for (const example of document.querySelectorAll(".examples button")) {
    example.addEventListener("click", () => {
      $("question").value = example.dataset.question;
      updateCount();
      $("ask").requestSubmit();
    });
  }
  $("ask").addEventListener("submit", async (event) => {
    event.preventDefault();
    const question = $("question").value.trim();
    if (!question || $("submit").disabled) return;
    $("submit").disabled = true;
    $("submit").textContent = "Asking...";
    document.body.classList.add("firing");
    setStatus("Searching the papers and writing an answer...");
    try {
      await ask(question);
    } finally {
      $("submit").disabled = false;
      $("submit").textContent = "Ask";
      document.body.classList.remove("firing");
    }
  });

  try {
    const response = await fetch("config.json", { cache: "no-store" });
    if (!response.ok) throw new Error(`config.json returned ${response.status}`);
    config = await response.json();
  } catch (error) {
    setStatus(`The page couldn't load its configuration (${error.message}).`, true);
    render();
    return;
  }

  const params = new URLSearchParams(window.location.search);
  try {
    if (params.has("error")) {
      window.history.replaceState(null, "", "/");
      throw new Error(`Sign-in failed: ${params.get("error_description") || params.get("error")}.`);
    }
    if (params.has("code")) await finishSignIn(params);
  } catch (error) {
    setStatus(error.message, true);
  }
  render();
  if (!accessToken() && !$("status").textContent) {
    setStatus("Sign in to ask a question. Accounts are created by the project owner.");
  }
}

init();
