define e = Character("エレン", color="#c8ffc8")
define m = Character("ミア", color="#ffc8c8")

label start:
    scene bg town
    "小さな村の朝。鳥のさえずりが聞こえる。"
    e "おはよう、[player_name]。よく眠れた？"
    m "お姉ちゃん、早くして！市場が始まっちゃうよ。"

    menu:
        "市場へ行く":
            jump market
        "家に残る":
            jump stay_home

label market:
    e "よし、市場に行こう。何かいいものが見つかるといいね。"
    m "あっ、あそこに武器屋さんがある！"
    "二人は市場の人混みの中へ消えていった。"
    return

label stay_home:
    e "今日は家でゆっくり休もう。"
    m "えー、つまらないの！"
    return
