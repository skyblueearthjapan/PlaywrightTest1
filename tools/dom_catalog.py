"""
DOMカタログ生成ツール（Python + Playwright版）

画面内の操作対象（ボタン/入力/リンク等）を機械的に抽出し、
安定セレクタ候補付きでJSON化します。

使い方:
    # 基本
    python tools/dom_catalog.py --url "https://..." --out "catalogs/page.json"

    # ブラウザ表示あり（デバッグ用）
    python tools/dom_catalog.py --url "https://..." --headed

    # ログイン状態を使用
    python tools/dom_catalog.py --url "https://..." --state "state.json"

    # 追加操作（モーダルを開く等）
    python tools/dom_catalog.py --url "https://..." --open firstVisit
"""

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError


def ensure_dir(p: Path) -> None:
    """ディレクトリが存在しなければ作成"""
    p.mkdir(parents=True, exist_ok=True)


def timestamp_dir(base: str = "artifacts") -> Path:
    """タイムスタンプ付きのディレクトリパスを生成"""
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    p = Path(base) / ts
    ensure_dir(p)
    return p


def save_artifacts(page, out_dir: Path) -> None:
    """失敗時の証拠を保存（スクリーンショット、HTML）"""
    ensure_dir(out_dir)
    page.screenshot(path=str(out_dir / "screenshot.png"), full_page=True)
    html = page.content()
    (out_dir / "page.html").write_text(html, encoding="utf-8")


def open_target(page, open_mode: str | None) -> None:
    """
    画面固有の追加操作をここに集約。
    例: 週表示→最初の予定をクリックして詳細モーダルを開く など。
    プロジェクトに合わせてカスタマイズしてください。
    """
    if not open_mode:
        return

    if open_mode == "firstVisit":
        # 最初の予定カードをクリックして詳細モーダルを開く例
        # data-testid等があればそれを優先
        page.locator("[data-visit-card]").first.click()
        page.get_by_role("dialog").wait_for(state="visible", timeout=10_000)
        return

    if open_mode == "createVisit":
        # 「追加」「新規」などのボタンをクリック
        page.get_by_role("button", name="追加").click()
        return

    if open_mode == "receipt":
        # レセプトボタンをクリック
        page.click("text=レセプト")
        page.wait_for_load_state("networkidle")
        return

    if open_mode == "visitNursing":
        # 訪問看護リンクをクリック
        page.click("text=訪問看護/1260192047")
        page.wait_for_load_state("networkidle")
        return

    raise ValueError(f"Unknown --open mode: {open_mode}")


def extract_catalog(page) -> list[dict]:
    """
    画面の「操作対象」だけ抽出して、安定セレクタ候補（selectorHint）を作る。
    """
    return page.evaluate(
        """
() => {
  const pickAttr = (el, names) => {
    for (const n of names) {
      const v = el.getAttribute?.(n);
      if (v) return v;
    }
    return "";
  };

  const isVisible = (el) => {
    const st = window.getComputedStyle(el);
    if (!st) return false;
    if (st.display === "none" || st.visibility === "hidden" || st.opacity === "0") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  const guessLabel = (el) => {
    const id = el.getAttribute?.("id");
    if (id) {
      const l = document.querySelector(`label[for="${CSS.escape(id)}"]`);
      if (l) return l.textContent?.trim() || "";
    }
    const parentLabel = el.closest?.("label");
    if (parentLabel) return parentLabel.textContent?.trim() || "";
    return "";
  };

  const roleOf = (el) => {
    const r = el.getAttribute("role");
    if (r) return r;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (tag === "button") return "button";
    if (tag === "a") return "link";
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "input") {
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "button" || type === "submit" || type === "image") return "button";
      return "textbox";
    }
    return tag;
  };

  const accName = (el) => {
    const aria = el.getAttribute("aria-label");
    if (aria) return aria.trim();

    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const parts = labelledBy.split(/\\s+/).map((id) => document.getElementById(id)?.textContent?.trim() || "");
      const joined = parts.filter(Boolean).join(" ").trim();
      if (joined) return joined;
    }

    // テキストコンテンツ（短いもののみ）
    const t = (el.textContent || "").trim();
    if (t && t.length < 100) return t;

    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (tag === "input" && type !== "password") {
      const v = el.getAttribute("value");
      if (v) return v.trim();
    }

    // alt属性（画像ボタン用）
    const alt = el.getAttribute("alt");
    if (alt) return alt.trim();

    return "";
  };

  const makeHint = (el, role, name, label, placeholder, testid) => {
    // 優先順位: testid > role+name > label > placeholder > id > name
    if (testid) return `page.get_by_test_id(${JSON.stringify(testid)})`;
    if (name && (role === "button" || role === "link" || role === "tab")) {
      return `page.get_by_role("${role}", name=${JSON.stringify(name)})`;
    }
    if (label) return `page.get_by_label(${JSON.stringify(label)})`;
    if (placeholder) return `page.get_by_placeholder(${JSON.stringify(placeholder)})`;
    const id = el.getAttribute("id");
    if (id) return `page.locator("#${id.replace(/:/g, '\\\\:').replace(/"/g, '\\"')}")`;
    const nm = el.getAttribute("name");
    if (nm) return `page.locator('[name="${nm.replace(/"/g, '\\"')}"]')`;
    return "";
  };

  const candidates = Array.from(document.querySelectorAll(
    'button, a, input, textarea, select, [role="button"], [role="link"], [role="textbox"], [role="combobox"], [role="tab"], [role="dialog"]'
  )).filter(isVisible);

  const items = candidates.map((el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    const role = roleOf(el);
    const name = accName(el);
    const label = guessLabel(el);
    const placeholder = el.getAttribute("placeholder") || "";
    const testid = pickAttr(el, ["data-testid", "data-test", "data-qa", "data-cy"]);
    const id = el.getAttribute("id") || "";
    const nm = el.getAttribute("name") || "";
    const ariaLabel = el.getAttribute("aria-label") || "";
    const rect = el.getBoundingClientRect();

    return {
      role,
      tag,
      type,
      name: name.substring(0, 200),  // 長すぎる場合は切り詰め
      label,
      placeholder,
      attrs: { id, name: nm, testid, ariaLabel },
      bbox: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
      selectorHint: makeHint(el, role, name, label, placeholder, testid),
    };
  });

  // selectorHintがあるものを上に、さらにroleでソート
  items.sort((a, b) => {
    if (b.selectorHint && !a.selectorHint) return 1;
    if (a.selectorHint && !b.selectorHint) return -1;
    return 0;
  });
  return items;
}
"""
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="DOMカタログ生成ツール")
    ap.add_argument("--url", required=True, help="対象URL")
    ap.add_argument("--out", default="catalog.json", help="出力ファイルパス")
    ap.add_argument("--open", default=None, help="追加操作モード (firstVisit/createVisit/receipt/visitNursing)")
    ap.add_argument("--headed", action="store_true", help="ブラウザを表示する")
    ap.add_argument("--state", default=None, help="storage_state JSONファイルのパス")
    args = ap.parse_args()

    out_path = Path(args.out)
    if out_path.parent != Path("."):
        ensure_dir(out_path.parent)

    with sync_playwright() as p:
        # Firefox を使用（Windows環境でより安定）
        browser = p.firefox.launch(headless=not args.headed)

        # storage_state を使う場合（ログイン済み状態を再利用）
        if args.state and Path(args.state).exists():
            print(f"ログイン状態を復元しています: {args.state}")
            context = browser.new_context(storage_state=args.state)
        else:
            context = browser.new_context()

        page = context.new_page()
        artifacts_dir = timestamp_dir("artifacts")

        try:
            print(f"ページを開いています: {args.url}")
            page.goto(args.url, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")

            # 必要に応じて追加操作（モーダルを開く等）
            if args.open:
                print(f"追加操作を実行しています: {args.open}")
                open_target(page, args.open)
                page.wait_for_timeout(2000)

            print("DOMカタログを抽出しています...")
            payload = {
                "url": page.url,
                "title": page.title(),
                "capturedAt": datetime.now().isoformat(),
                "openMode": args.open,
                "items": extract_catalog(page),
            }

            out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"保存しました: {out_path} (要素数={len(payload['items'])})")

        except Exception as e:
            print(f"カタログ生成に失敗しました: {repr(e)}")
            # 失敗時の証拠を保存
            save_artifacts(page, artifacts_dir)

            # 可能ならカタログも保存（途中でも役に立つ）
            try:
                payload = {
                    "url": page.url,
                    "title": page.title(),
                    "capturedAt": datetime.now().isoformat(),
                    "openMode": args.open,
                    "items": extract_catalog(page),
                    "error": str(e),
                }
                (artifacts_dir / "catalog.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception:
                pass

            print(f"アーティファクトを保存しました: {artifacts_dir}")
            raise
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
