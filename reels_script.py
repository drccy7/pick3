"""Instagram Reels / Shorts 15-second series script generator for Pick3."""

from __future__ import annotations


def build_reels_script(recommendation: dict, episode: int = 1) -> dict:
    """Convert a Pick3 recommendation payload into a 15-20s Reels series script."""
    query = recommendation.get("query", "맛집")
    area = recommendation.get("area", "내 주변")
    sit_label = recommendation.get("situation_label", "추천")
    top = recommendation.get("top_pick") or {}
    alts = recommendation.get("alternatives") or []
    places = [top, *alts][:3]

    title = f"[{query} 지도 #{episode}] {area}에서 실패 없는 딱 3곳"
    hook = f"{area}에서 {query} 어디 갈지 10분 넘게 고른다면 저장하세요. ({sit_label} 기준)"

    cuts: list[dict] = []
    time_slots = ["2~6초", "6~10초", "10~14초"]
    default_roles = ["종합 1위", "가성비/가까운 대안", "분위기/만족 대안"]

    for idx, p in enumerate(places):
        name = p.get("name", f"후보 {idx + 1}")
        role = p.get("role_badge") or default_roles[idx]
        reason = p.get("reason", "")
        price = p.get("price_note", "")
        caution = p.get("caution_note", "")

        on_screen = f"{idx + 1}. {name} ({role})"
        voice_parts = [f"{idx + 1}번째, {name}."]
        if reason:
            voice_parts.append(reason + ".")
        if price and "확인" not in price:
            voice_parts.append(price + ".")
        if caution:
            voice_parts.append(caution.replace("⏰ ", "") + ".")

        cuts.append(
            {
                "order": idx + 1,
                "time": time_slots[idx] if idx < len(time_slots) else "10~14초",
                "role": role,
                "place_name": name,
                "on_screen_caption": on_screen,
                "price_line": price,
                "caution_line": caution,
                "voiceover": " ".join(voice_parts),
            }
        )

    outro = (
        f"다음 #{episode + 1}편은 댓글에 가장 많이 나온 동네로 갑니다! "
        f"내 동네 놓치지 않으려면 팔로우해 두세요."
    )

    area_tag = area.replace(" ", "").replace("/", "")
    menu_tag = query.replace(" ", "")
    lines = [
        f"🎬 {title}",
        "",
        f"[0~2초 훅] {hook}",
        "",
    ]
    for c in cuts:
        lines.append(f"[{c['time']} | {c['on_screen_caption']}]")
        lines.append(f"  🎙️ {c['voiceover']}")
        if c["price_line"]:
            lines.append(f"  💰 {c['price_line']}")
        if c["caution_line"]:
            lines.append(f"  {c['caution_line']}")
    lines.extend(
        [
            "",
            f"[14~18초 엔딩 CTA] {outro}",
            "",
            "--- 인스타 본문 캡션 ---",
            f"{area} {query} 결정 끝! ({sit_label})",
            *[f"{c['order']}. {c['place_name']} - {c['price_line'] or c['role']}" for c in cuts],
            "",
            f"다음 편 원하는 동네를 댓글로 남겨주세요 👇",
            f"#{area_tag}맛집 #{menu_tag} #{area_tag}{menu_tag} #맛집추천 #결정장애해결",
        ]
    )

    return {
        "episode": episode,
        "title": title,
        "hook_0_2s": hook,
        "cuts": cuts,
        "outro_cta_14_18s": outro,
        "caption_and_hashtags": "\n".join(lines[-5:]),
        "full_text": "\n".join(lines),
    }
