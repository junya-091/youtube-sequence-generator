// set_v2_scale.jsx
// Premiere Pro ExtendScript
// V3 トラックの調整レイヤーに手動で追加済みの「トランスフォーム」エフェクトの
// スケールを 115% に設定する
// ※ 事前に全調整レイヤーに「トランスフォーム」エフェクトを手動で追加しておくこと

var SCALE_VALUE = 115;
var TRACK_INDEX = 2; // V3 (0-indexed: V1=0, V2=1, V3=2)
var TRANSFORM_MATCH_NAME = "ADBE Geometry2";

var seq = app.project.activeSequence;
if (!seq) {
    alert("エラー: アクティブなシーケンスが見つかりません。");
} else {
    var track = seq.videoTracks[TRACK_INDEX];
    if (!track || track.clips.numItems === 0) {
        alert("エラー: V" + (TRACK_INDEX + 1) + " トラックにクリップが見つかりません。");
    } else {
        var successCount = 0;
        var noEffectCount = 0;
        var errInfo = "";

        for (var i = 0; i < track.clips.numItems; i++) {
            var clip = track.clips[i];

            // 「トランスフォーム」コンポーネントを探す
            var transformComp = null;
            for (var j = 0; j < clip.components.numItems; j++) {
                if (clip.components[j].matchName === TRANSFORM_MATCH_NAME) {
                    transformComp = clip.components[j];
                    break;
                }
            }

            if (!transformComp) {
                noEffectCount++;
                errInfo += "クリップ " + i + " (" + clip.name + "): トランスフォームエフェクトが見つかりません\n";
                continue;
            }

            // スケールプロパティを設定
            var scaleSet = false;
            for (var k = 0; k < transformComp.properties.numItems; k++) {
                var prop = transformComp.properties[k];
                var isScale = (prop.displayName === "Scale")
                           || (prop.displayName === "スケール")
                           || (prop.matchName === "ADBE Scale")
                           || (prop.matchName === "ADBE Geom Scale");
                if (!isScale) continue;
                try {
                    prop.setValue(SCALE_VALUE, true);
                    scaleSet = true;
                } catch(e) {
                    errInfo += "クリップ " + i + ": setValue 失敗 (" + e.message + ")\n";
                }
                break;
            }

            if (scaleSet) {
                successCount++;
            } else {
                var propList = "";
                for (var p = 0; p < transformComp.properties.numItems; p++) {
                    propList += "[" + p + "] " + transformComp.properties[p].displayName
                             + " / matchName=" + transformComp.properties[p].matchName + "\n";
                }
                errInfo += "クリップ " + i + ": スケールプロパティ見つからず\n" + propList;
            }
        }

        var msg = successCount + " / " + track.clips.numItems + " 個のトランスフォーム スケールを " + SCALE_VALUE + "% に設定しました。";
        if (noEffectCount > 0) {
            msg += "\n\n⚠ " + noEffectCount + " 個はトランスフォームエフェクト未追加です。\n"
                 + "手順: 全調整レイヤーを選択 → エフェクトパネルで「トランスフォーム」を検索 → ドラッグ適用 → 再実行";
        }
        if (errInfo) msg += "\n\n[詳細]\n" + errInfo;
        alert(msg);
    }
}
