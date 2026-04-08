// apply_zoom.jsx  v1
// Premiere Pro ExtendScript
// V2 Slug → 調整レイヤー置換 + トランスフォーム追加 + スケール115% を一括実行
//
// 使い方:
//   1. YouTube Sequence をアクティブにする
//   2. ExtendScript Toolkit / Loader Script Panel で実行

var SCALE_VALUE = 115;
var TRACK_INDEX = 1; // V2 (0-indexed: V1=0, V2=1)
var TRANSFORM_MATCH_NAME = "ADBE Geometry2";

// ── プロジェクトツリーから調整レイヤーを再帰検索 ──
function findAdjItem(folder) {
    for (var i = 0; i < folder.children.numItems; i++) {
        var item = folder.children[i];
        try {
            if (item.type !== ProjectItemType.BIN && item.name === "調整レイヤー") {
                return item;
            }
        } catch (e) {}
        if (item.type === ProjectItemType.BIN) {
            var found = findAdjItem(item);
            if (found) return found;
        }
    }
    return null;
}

// ── メイン ──
(function () {
    var seq = app.project.activeSequence;
    if (!seq) {
        alert("エラー: アクティブなシーケンスが見つかりません。\nYouTube Sequence を開いてください。");
        return;
    }

    var adjItem = findAdjItem(app.project.rootItem);
    if (!adjItem) {
        alert("エラー: プロジェクトに「調整レイヤー」アイテムが見つかりません。\n" +
              "プロジェクトパネルで 新規アイテム → 調整レイヤー を作成してください。");
        return;
    }

    var track = seq.videoTracks[TRACK_INDEX];
    if (!track || track.clips.numItems === 0) {
        alert("V2 にクリップがありません。sequence.xml をインポート済みか確認してください。");
        return;
    }

    var report = "";

    // ====== Part A: V2 Slug → 調整レイヤー置換 ======
    report += "=== Part A: 調整レイヤー置換 ===\n";

    // 1. クリップ情報を収集
    var infos = [];
    for (var i = 0; i < track.clips.numItems; i++) {
        var c = track.clips[i];
        infos.push({
            startSec: c.start.seconds,
            endSec: c.end.seconds,
            durSec: c.duration.seconds
        });
    }
    report += "対象: " + infos.length + " クリップ\n";

    // 2. 後ろから全クリップ削除
    for (var i = track.clips.numItems - 1; i >= 0; i--) {
        track.clips[i].remove(false, false);
    }

    // 3. 調整レイヤーを overwriteClip で配置
    var placeErr = "";
    for (var i = 0; i < infos.length; i++) {
        try {
            var t = new Time();
            t.seconds = infos[i].startSec;
            seq.overwriteClip(adjItem, t, TRACK_INDEX, -1);
        } catch (e) {
            placeErr += "[" + i + "] 配置失敗: " + e.message + "\n";
        }
    }

    // 4. 長さ調整
    for (var i = 0; i < track.clips.numItems; i++) {
        var clip = track.clips[i];
        for (var j = 0; j < infos.length; j++) {
            if (Math.abs(clip.start.seconds - infos[j].startSec) < 0.05) {
                try {
                    var et = new Time();
                    et.seconds = infos[j].endSec;
                    clip.end = et;
                } catch (e) {}
                break;
            }
        }
    }

    report += "配置後クリップ数: " + track.clips.numItems + "\n";
    if (placeErr) report += "[配置エラー]\n" + placeErr;

    // ====== Part B: トランスフォームエフェクト追加 ======
    report += "\n=== Part B: トランスフォーム追加 ===\n";

    var qeSuccess = false;
    try {
        // QE API でエフェクト追加を試行
        var qeSeq = qe.project.getActiveSequence();
        if (qeSeq) {
            var qeTrack = qeSeq.getVideoTrackAt(TRACK_INDEX);
            if (qeTrack) {
                var addCount = 0;
                for (var i = 0; i < qeTrack.numItems; i++) {
                    try {
                        var qeItem = qeTrack.getItemAt(i);
                        qeItem.addVideoEffect(qe.project.getVideoEffectByName("Transform"));
                        addCount++;
                    } catch (e2) {}
                }
                if (addCount > 0) {
                    qeSuccess = true;
                    report += "QE API でトランスフォーム追加: " + addCount + " クリップ\n";
                }
            }
        }
    } catch (e) {
        // QE API 非対応
    }

    if (!qeSuccess) {
        report += "⚠ QE API 非対応 / 失敗\n";
        report += "手動操作が必要です:\n";
        report += "  1. V2 の全調整レイヤーを選択\n";
        report += "  2. エフェクトパネルで「トランスフォーム」を検索\n";
        report += "  3. ドラッグで適用\n";
        report += "  4. 再度このスクリプトを実行（Part C のみ動作）\n";
    }

    // ====== Part C: スケール 115% 設定 ======
    report += "\n=== Part C: スケール " + SCALE_VALUE + "% ===\n";

    var scaleSuccess = 0;
    var scaleNoEffect = 0;
    var scaleErr = "";

    for (var i = 0; i < track.clips.numItems; i++) {
        var clip = track.clips[i];

        // トランスフォームコンポーネントを探す
        var transformComp = null;
        for (var j = 0; j < clip.components.numItems; j++) {
            if (clip.components[j].matchName === TRANSFORM_MATCH_NAME) {
                transformComp = clip.components[j];
                break;
            }
        }

        if (!transformComp) {
            scaleNoEffect++;
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
            } catch (e) {
                scaleErr += "クリップ " + i + ": setValue 失敗 (" + e.message + ")\n";
            }
            break;
        }

        if (scaleSet) scaleSuccess++;
    }

    report += "スケール設定: " + scaleSuccess + " / " + track.clips.numItems + " 成功\n";
    if (scaleNoEffect > 0) {
        report += "⚠ トランスフォーム未追加: " + scaleNoEffect + " クリップ\n";
    }
    if (scaleErr) report += "[エラー]\n" + scaleErr;

    // ====== 完了レポート ======
    report += "\n✅ 完了";
    alert(report);
})();
