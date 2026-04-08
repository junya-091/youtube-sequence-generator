// delete_markers.jsx  v1
// Premiere Pro ExtendScript
// アクティブシーケンスの全マーカー（または名前フィルタ付き）を一括削除
//
// 使い方:
//   1. YouTube Sequence をアクティブにする
//   2. 実行 → 削除対象を選択
//   3. マーカーが削除される

(function () {
    var seq = app.project.activeSequence;
    if (!seq) {
        alert("エラー: アクティブなシーケンスが見つかりません。");
        return;
    }

    var markers = seq.markers;
    if (markers.numMarkers === 0) {
        alert("シーケンスにマーカーがありません。");
        return;
    }

    // 削除モード選択
    var mode = prompt(
        "削除モードを選択してください:\n\n" +
        "  all     - 全マーカー削除\n" +
        "  zoom    - ZOOM マーカーのみ\n" +
        "  sfx     - SFX: マーカーのみ\n" +
        "  tr      - TR: マーカーのみ\n" +
        "  insert  - INSERT マーカーのみ\n" +
        "  telop   - テロップカテゴリマーカーのみ\n\n" +
        "入力 (デフォルト: all):",
        "all"
    );

    if (mode === null) return; // キャンセル
    mode = mode.toLowerCase().replace(/^\s+|\s+$/g, "") || "all";

    var telopCategories = ["強調", "ネガティブ", "ポジティブ", "ツッコミ", "通常"];

    // マーカーを収集（後ろから削除するため）
    var toDelete = [];
    var marker = markers.getFirstMarker();
    while (marker) {
        var name = marker.name || "";
        var shouldDelete = false;

        if (mode === "all") {
            shouldDelete = true;
        } else if (mode === "zoom") {
            shouldDelete = (name === "ZOOM");
        } else if (mode === "sfx") {
            shouldDelete = (name.indexOf("SFX:") === 0);
        } else if (mode === "tr") {
            shouldDelete = (name.indexOf("TR:") === 0);
        } else if (mode === "insert") {
            shouldDelete = (name === "INSERT");
        } else if (mode === "telop") {
            for (var i = 0; i < telopCategories.length; i++) {
                if (name === telopCategories[i]) {
                    shouldDelete = true;
                    break;
                }
            }
        }

        if (shouldDelete) {
            toDelete.push(marker);
        }
        marker = markers.getNextMarker(marker);
    }

    if (toDelete.length === 0) {
        alert("削除対象のマーカーがありません (フィルタ: " + mode + ")。");
        return;
    }

    // 確認
    var confirm = prompt(
        toDelete.length + " 個のマーカーを削除します。\n" +
        "フィルタ: " + mode + "\n\n" +
        "「yes」を入力して続行:",
        ""
    );

    if (confirm !== "yes") {
        alert("キャンセルしました。");
        return;
    }

    // 削除実行（後ろから）
    var deleted = 0;
    for (var i = toDelete.length - 1; i >= 0; i--) {
        try {
            markers.deleteMarker(toDelete[i]);
            deleted++;
        } catch (e) {}
    }

    alert("✅ マーカー削除完了\n" +
          "フィルタ: " + mode + "\n" +
          "削除数: " + deleted + " / " + toDelete.length);
})();
