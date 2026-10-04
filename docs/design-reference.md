# 보고서 디자인 참고 (사용자 제공, 2026-10-05)

사용자가 보고서 디자인 기준으로 제공한 문서다. claude.com 마케팅 사이트의 시각 언어(크림 캔버스, 코랄 포인트,
세리프 디스플레이, 다크 네이비 표면)를 묘사한다. Smith 보고서는 **색·서체·여백·카드 구성 등 스타일만** 차용한다.
Anthropic 스파이크 마크, "Claude"·"Anthropic" 워드마크 등 상표 요소는 쓰지 않으며 Smith 자체 표기를 쓴다.
메일 환경 적용 방식은 `docs/report-design.md` §11을 따른다.

---

## Overview

Claude.com is the warmest, most editorial interface in the AI-product category. The base atmosphere is a **tinted cream canvas** (`{colors.canvas}` — #faf9f5) — distinctly warm, deliberately not the cool gray-white that every other AI brand uses. Headlines run a **slab-serif display** ("Copernicus" / Tiempos Headline) at weight 400 with negative letter-spacing, paired with **StyreneB / Inter** body sans. The combination feels like a literary publication, not a SaaS marketing page.

Brand voltage comes from the **cream + coral pairing** — coral (`{colors.primary}` — #cc785c) is the signature accent, used on every primary CTA, on the brand wordmark, and on full-bleed callout cards. The coral is warm, slightly muted, never cyan/blue.

Three surface modes alternate: **cream canvas** (#faf9f5, default floor), **light cream cards** (#efe9de, feature cards), **dark navy product surfaces** (#181715, mockups, showcase cards, footer). The cream-to-dark contrast is the page's pacing rhythm.

Key characteristics: warm cream canvas with dark warm-ink text (#141413); coral primary (#cc785c) used scarcely on elements, generously on full-bleed callouts; slab-serif display at weight 400 with negative tracking paired with humanist sans body; dark navy cards (#181715); light cream cards (#efe9de); hierarchical radius (8px buttons/inputs, 12px cards, 16px hero container, pill badges); section rhythm 96px; card padding 32px.

## Colors

- Brand & accent: primary coral #cc785c; primary-active #a9583e; primary-disabled #e6dfd8; accent-teal #5db8a6 (sparing); accent-amber #e8a55a (badges, inline highlights).
- Surface: canvas #faf9f5; surface-soft #f5f0e8; surface-card #efe9de; surface-cream-strong #e8e0d2; surface-dark #181715; surface-dark-elevated #252320; surface-dark-soft #1f1e1b; hairline #e6dfd8; hairline-soft #ebe6df.
- Text: ink #141413; body-strong #252523; body #3d3d3a; muted #6c6a64; muted-soft #8e8b82; on-primary #ffffff; on-dark #faf9f5; on-dark-soft #a09d96.
- Semantic: success #5db872; warning #d4a017; error #c64545.

## Typography

Display: Copernicus / Tiempos Headline (fallback Cormorant Garamond 500 with -0.02em, EB Garamond, Garamond, "Times New Roman", serif), weight 400, negative letter-spacing, never bold. Body: StyreneB / Inter (fallback -apple-system, "Segoe UI", Roboto, sans-serif), 400 for paragraphs, 500 for labels. Code: JetBrains Mono.

| Token | Size | Weight | Line height | Letter spacing |
|---|---|---|---|---|
| display-xl | 64px | 400 | 1.05 | -1.5px |
| display-lg | 48px | 400 | 1.1 | -1px |
| display-md | 36px | 400 | 1.15 | -0.5px |
| display-sm | 28px | 400 | 1.2 | -0.3px |
| title-lg | 22px | 500 | 1.3 | 0 |
| title-md | 18px | 500 | 1.4 | 0 |
| title-sm | 16px | 500 | 1.4 | 0 |
| body-md | 16px | 400 | 1.55 | 0 |
| body-sm | 14px | 400 | 1.55 | 0 |
| caption | 13px | 500 | 1.4 | 0 |
| caption-uppercase | 12px | 500 | 1.4 | 1.5px |

## Layout, elevation, shapes

- Spacing base 4px: 4 · 8 · 12 · 16 · 24 · 32 · 48 · 96 (section). Card padding 32px; callout bands 48px.
- Elevation is color-block first, shadow rare (`0 1px 3px rgba(20,20,19,0.08)` at most). Hairline 1px #e6dfd8 borders.
- Radius: 4 / 6 / 8 (buttons, inputs) / 12 (cards) / 16 (large containers) / pill.

## Components used by reports

- feature-card: surface-card background, 12px radius, 32px padding, title-md heading, body-md text.
- product-mockup-card-dark: surface-dark background, on-dark text, 12px radius, 32px padding.
- callout-card-coral: primary background, on-primary text, 12px radius, 48px padding — reserved for the single most important message.
- badge-pill: surface-card background, caption type, pill radius; badge-coral for highlights.
- Pacing: alternate cream → cream-card → dark → cream → coral callout → dark footer; never the same surface twice in a row.

## Do / Don't

Do anchor on the cream canvas; use the serif for display headings with negative tracking; reserve coral for the primary action and full-bleed callouts; alternate cream and dark bands. Don't use cool grays or pure white canvas; don't bold the serif; don't use cool blue or cyan as the brand accent; don't put coral everywhere; don't introduce a fourth surface tone.

(The full original text, including responsive breakpoints and the complete component list, was supplied by the user in the 2026-10-05 conversation; only the parts relevant to an email report are kept here.)
