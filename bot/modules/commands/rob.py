import asyncio
import random
from asyncio import Lock

from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot import bot, prefixes, game, sakura_b
from bot.func_helper.msg_utils import deleteMessage, editMessage
from bot.sql_helper.sql_emby import sql_get_emby, sql_update_emby, Emby

# ==========================================
# 辅助函数：智能取整
# ==========================================
def to_int(value):
    """
    将数值(含小数)四舍五入转为整数，且最小值为1
    防止计算出 0 或小数导致报错
    """
    return max(1, int(round(value)))

# ==========================================
# 游戏平衡配置
# ==========================================
# 基础倍率转换
BASE_MAG = float(game.magnification)

# --- 核心机制 ---
# 抢劫门票费
COMMISSION_FEE = to_int(BASE_MAG * 1.0)

# 单次最大抢劫金额硬上限
MAX_ROB_LIMIT = to_int(BASE_MAG * 5.0)

# 资产保护比例
ROB_PERCENT_LIMIT = 0.10 

# 最小抢劫对象门槛
MIN_ROB_TARGET = to_int(BASE_MAG * 3.0)

# 战斗失败惩罚
FIGHT_PENALTY = to_int(BASE_MAG * 2.0)

# 抢劫持续时间
ROB_TIME = 5

# --- 围观群众 配置 ---
# 围观奖励总池：每场抢劫系统发放给群众的总福利
TOTAL_GAME_COINS = to_int(BASE_MAG * 2.0)

# 概率配置
PENALTY_CHANCE = 10   # 倒霉蛋概率
BONUS_CHANCE = 25     # 幸运儿概率

# 围观数值
PENALTY_AMOUNT = to_int(BASE_MAG * 0.5)      # 倒霉蛋扣除
BONUS_MIN_AMOUNT = to_int(BASE_MAG * 0.5)    # 捡钱最小值
BONUS_MAX_AMOUNT = to_int(BASE_MAG * 1.5)    # 捡钱最大值
LUCKY_AMOUNT = to_int(BASE_MAG * 3.0)        # 幸运大奖

rob_games = {}
rob_locks = {}

def get_lock(key):
    if key not in rob_locks:
        rob_locks[key] = Lock()
    return rob_locks[key]

async def delete_msg_with_error(message, error_text):
    error_message = await bot.send_message(message.chat.id, error_text, reply_to_message_id=message.id)
    asyncio.create_task(deleteMessage(error_message, 180))
    asyncio.create_task(deleteMessage(message, 180))

def change_emby_amount(user_id, amount):
    final_amount = max(0, int(amount))
    sql_update_emby(Emby.tg == user_id, iv=final_amount)

async def countdown(call, rob_message):
    while True:
        await asyncio.sleep(60)
        if rob_message.id in rob_games:
            game = rob_games[rob_message.id]
            game['remaining_time'] -= 1
            await update_edit_message(call, game)
            if game['remaining_time'] <= 0:
                 break
        else:
            break

async def start_rob(message, user, target_user):
    narrative_msg = await bot.send_message(
        message.chat.id,
        f"1899年，西部荒野已逐渐消失，昔日的亡命之徒正面临覆灭。\n然而，仍有一群亡命之徒不甘寂寞，四处作乱，抢劫为生……\n\n🕵️‍♂️ 事件系统正在初始化...",
        reply_to_message_id=message.id
    )

    await asyncio.sleep(2)
    await deleteMessage(narrative_msg)

    global rob_games

    percent_limit = target_user.iv * ROB_PERCENT_LIMIT
    max_safe_rob = min(percent_limit, MAX_ROB_LIMIT)
    final_max_rob = max(1, to_int(max_safe_rob))
    rob_amount = random.randint(1, final_max_rob)
    
    user_with_link = await get_fullname_with_link(user.tg)
    target_with_link = await get_fullname_with_link(target_user.tg)
    
    keyboard_rob = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                text='💸 破财消灾',
                callback_data=f'rob_flee_{rob_amount}_{user.tg}_{target_user.tg}'
            ),
            InlineKeyboardButton(
                text='⚔️ 拼死反抗',
                callback_data=f'rob_fight_{rob_amount}_{user.tg}_{target_user.tg}'
            )
        ],
        [
            InlineKeyboardButton(
                text='🍿 搬好小板凳',
                callback_data=f'rob_kanxi_{rob_amount}_{user.tg}_{target_user.tg}'
            )
        ]
    ])

    rob_prepare_text = (
        f"· 【抢劫事件】\n\n"
        f"· 🥷 委托雇主 | {user_with_link}\n"
        f"· ⚔️ 抢劫目标 | {target_with_link}\n"
        f"· 💵 劫掠金额 | {rob_amount}\n"
        f"· ⏳ 剩余时间 | 5 分钟\n"
        f"· 🔥 战斗回合 | ROUND 0\n\n"
        f"· 🧨 乱世的盗贼 : 等待投点\n"
        f"· VS\n"
        f"· 🛡️ {target_with_link} : 等待投点\n\n"
        f"· 📺 围观群众:\n"
    )
    rob_message = await bot.send_message(
        message.chat.id,
        rob_prepare_text,
        reply_to_message_id=message.id,
        reply_markup=keyboard_rob
    )
    rob_games[rob_message.id] = {
        "target_user_id": target_user.tg,
        "user_id": user.tg,
        "rob_gold": rob_amount,
        "rob_prepare_text": rob_prepare_text,
        "kanxi_list": [],
        "round_time": 0,
        "user_score": 0,
        "target_score": 0,
        "kanxi_name": "",
        "rob_msg_id": rob_message.id,
        "original_message": rob_message,
        "remaining_time": ROB_TIME, 
        "chat_id": message.chat.id
    }

    asyncio.create_task(countdown(message, rob_message))

async def show_onlooker_message(call, game):
    onlookers_messages = ["· 📺 围观群众"]
    if game['kanxi_list']:
        for kanxi_id in game['kanxi_list']:
            name = await get_fullname_with_link(kanxi_id)
            possible_messages = [
                f"· {name} 纷纷说道：这都啥……",
                f"· {name} 纷纷说道：板凳都搬来了……",
                f"· {name} 纷纷说道：就给我看这些……",
                f"· {name} 默默举起了瓜子袋：继续继续～",
                f"· {name} 低声嘀咕：快点打，我快下班了……",
                f"· {name} 兴奋喊道：谁输谁请客啊！",
                f"· {name} 忍不住笑出声：这操作我能笑一天",
                f"· {name} 悄悄录了个屏：以后留着当表情包",
                f"· {name} 满脸问号：我是不是进错群了？",
                f"· {name} 边看边说：我押十块，赌翻车！",
                f"· {name} 大喊：导演再来一条，这条不够劲！",
            ]
            selected_message = random.choice(possible_messages)
            onlookers_messages.append(selected_message)

    reward_message = "\n".join(onlookers_messages)
    reward_msg = await bot.send_message(game['chat_id'], reward_message, reply_to_message_id=game['rob_msg_id'])
    asyncio.create_task(deleteMessage(reward_msg, 180))

async def update_edit_message(call, game, status=None):
    user_with_link = await get_fullname_with_link(game['user_id'])
    target_with_link = await get_fullname_with_link(game['target_user_id'])
    user_score = '等待投点' if game['round_time'] == 0 else str(game['user_score']) + ' 分'
    target_score = '等待投点' if game['round_time'] == 0 else str(game['target_score']) + ' 分'
    update_text = (
        f"· 【抢劫事件】\n\n"
        f"· 🥷 委托雇主 | {user_with_link}\n"
        f"· ⚔️ 抢劫对象 | {target_with_link}\n"
        f"· 💵 劫掠金额 | {game['rob_gold']}\n"
        f"· ⏳ 剩余时间 | {game['remaining_time']} 分钟\n"
        f"· 🔥 战斗回合 | ROUND {game['round_time']}\n\n"
        f"· 🧨 乱世的盗贼 : {user_score}\n"
        f"· VS\n"
        f"· 🛡️ {target_with_link} : {target_score}\n\n"
    )

    if status == 'surrender':
        update_text += f"· 🎫 最终结果 | {user_with_link} 获胜！\n"
        user = sql_get_emby(game['user_id'])
        target_user = sql_get_emby(game['target_user_id'])
        
        if target_user.iv < game['rob_gold']:
            rob_gold = to_int(target_user.iv / 2)
        else:
            rob_gold = to_int(game['rob_gold'])
            
        actual_rob_gold = min(rob_gold, target_user.iv)
        
        change_emby_amount(game['target_user_id'], target_user.iv - actual_rob_gold)
        change_emby_amount(game['user_id'], user.iv + actual_rob_gold)

        await editMessage(game['original_message'], update_text)
        answer = f"🎉 对方投降了\n\n对方选择投降，乱世盗贼不战而胜\n获得：{actual_rob_gold} {sakura_b}\n余额：{user.iv + actual_rob_gold} {sakura_b}"

        await bot.send_message(user.tg, answer, reply_to_message_id=call.message.id)

        target_answer = f"😌 你投降了\n\n您向 {user_with_link} 的乱世盗贼投降\n割地赔款：{actual_rob_gold} {sakura_b}\n余额： {target_user.iv - actual_rob_gold} {sakura_b}️"
        await bot.send_message(target_user.tg, target_answer, reply_to_message_id=call.message.id)

        del rob_games[game['rob_msg_id']]
        return

    if game['remaining_time'] <= 0:
        buttons = []
        user = sql_get_emby(game['user_id'])
        target_user = sql_get_emby(game['target_user_id'])
        
        if game['round_time'] == 0:
            update_text += f"· 🎫 最终结果 | {target_with_link} 不在家！\n"
            await editMessage(game['original_message'], update_text, buttons)
            
            not_answer = f"{target_with_link} 没在家，乱世的盗贼白忙一场，{user_with_link} 只能眼睁睁看着佣金 💸 打水漂，啥也没捞到 🤡"
            no_answer_msg = await bot.send_message(call.chat.id, not_answer, reply_to_message_id=call.id)
            
            await bot.send_message(
                user.tg, 
                f"😌 抢劫失败\n\n{target_with_link} 没在家，乱世的盗贼白跑一趟\n失去佣金：{COMMISSION_FEE} {sakura_b}\n余额：{user.iv} {sakura_b}",
                reply_to_message_id=call.id
            )
            
            await bot.send_message(
                target_user.tg,
                f"🎉 逃过一杰\n\n{user_with_link} 尝试抢劫你，可惜你不在家\n余额：{target_user.iv} {sakura_b}",
                reply_to_message_id=call.id
            )

            await show_onlooker_message(call, game)
            asyncio.create_task(deleteMessage(game['original_message'], 180))
            asyncio.create_task(deleteMessage(no_answer_msg, 180))
            
        else:
            update_text += f"· 🎫 最终结果 | 时间到！按当前比分决定胜负\n"
            await editMessage(game['original_message'], update_text, buttons)
            
            if game["target_score"] > game["user_score"]:
                actual_penalty = min(user.iv, FIGHT_PENALTY)
                message = f"⏰ 时间到！{target_with_link} 以 {game['target_score']} : {game['user_score']} 获胜🏆\n{user_with_link} 失去 {actual_penalty} {sakura_b}😭"
                success_msg = await bot.send_message(call.chat.id, message, reply_to_message_id=call.id)
                asyncio.create_task(deleteMessage(success_msg, 180))
                
                change_emby_amount(user.tg, user.iv - actual_penalty)
                change_emby_amount(target_user.tg, target_user.iv + actual_penalty)
                
                await bot.send_message(
                    user.tg,
                    f"😌 抢劫失败\n\n时间到，抢劫失败\n损失：{actual_penalty} {sakura_b}\n余额：{sql_get_emby(user.tg).iv} {sakura_b}",
                    reply_to_message_id=call.id)
                    
                await bot.send_message(
                    target_user.tg,
                    f"🎉 防守成功\n\n时间到，你击败了盗贼\n获得：{actual_penalty} {sakura_b}\n余额：{sql_get_emby(target_user.tg).iv} {sakura_b}",
                    reply_to_message_id=call.id)
                    
            elif game["target_score"] < game["user_score"]:
                if target_user.iv < game['rob_gold']:
                    rob_gold = target_user.iv
                else:
                    rob_gold = to_int(game['rob_gold'])
                
                message = f"⏰ 时间到！{user_with_link} 以 {game['user_score']} : {game['target_score']} 获胜🏆\n{target_with_link} 损失 {rob_gold} {sakura_b}😭"
                
                await bot.send_message(
                    user.tg,
                    f"🎉 抢劫成功\n\n时间到，抢劫成功\n获得：{rob_gold} {sakura_b}\n余额：{user.iv + rob_gold} {sakura_b}",
                    reply_to_message_id=call.id
                )
                await bot.send_message(
                    target_user.tg,
                    f"😌 防守失败\n\n时间到，你败给了盗贼\n损失：{rob_gold} {sakura_b}\n余额：{target_user.iv - rob_gold} {sakura_b}",
                    reply_to_message_id=call.id
                )

                change_emby_amount(user.tg, user.iv + rob_gold)
                change_emby_amount(target_user.tg, target_user.iv - rob_gold)
                
                rob_msg = await bot.send_message(call.chat.id, message, reply_to_message_id=call.id)
                asyncio.create_task(deleteMessage(rob_msg, 180))
                
            else:
                message = f"⏰ 时间到！双方 {game['user_score']} : {game['target_score']} 打平了，乱世的盗贼跑路了，{user_with_link} 痛失佣金 💸"
                rob_msg = await bot.send_message(call.chat.id, message, reply_to_message_id=call.id)
                asyncio.create_task(deleteMessage(rob_msg, 180))
                
                await bot.send_message(
                    user.tg,
                    f"😌 抢劫失败\n\n打成平手\n损失：{COMMISSION_FEE} {sakura_b}\n余额：{user.iv} {sakura_b}！",
                    reply_to_message_id=call.id
                )
                await bot.send_message(
                    target_user.tg,
                    f"🎉 逃过一杰\n\n平手，成功保住财产\n余额：{target_user.iv} {sakura_b}！",
                    reply_to_message_id=call.id
                )
            
            asyncio.create_task(handle_kanxi_rewards(game))
            asyncio.create_task(deleteMessage(call, 180))
        
        del rob_games[game['rob_msg_id']]
    else:
        if game['round_time'] < 3:
            buttons = get_buttons(game)
            update_text += f"· 📺 围观群众:\n{game['kanxi_name']}"
            await editMessage(game['original_message'], update_text, buttons)
        else:
            await editMessage(game['original_message'], update_text)

def get_buttons(game):
    flee_button = InlineKeyboardButton(
        text='💸 破财免灾',
        callback_data=f'rob_flee_{game["rob_gold"]}_{game["user_id"]}_{game["target_user_id"]}'
    )
    fight_button = InlineKeyboardButton(
        text='⚔️ 拼死反抗',
        callback_data=f'rob_fight_{game["rob_gold"]}_{game["user_id"]}_{game["target_user_id"]}')
    kanxi_button = InlineKeyboardButton(
        text='📺 搬好小板凳',
        callback_data=f'rob_kanxi_{game["rob_gold"]}_{game["user_id"]}_{game["target_user_id"]}')
    return InlineKeyboardMarkup([[flee_button, fight_button], [kanxi_button]])

async def onlookers(call):
    game = rob_games[call.message.id]
    if call.from_user.id != int(call.data.split("_")[4]):
        kanxi_id = call.from_user.id
        if kanxi_id not in game['kanxi_list']:
            game['kanxi_list'].append(kanxi_id)
            name_ = await get_fullname_with_link(kanxi_id)

            funny_watch_lines = [
                f"· {name_} 正抱着瓜子围观中…",
                f"· {name_} 偷偷打开了录像机…",
                f"· {name_} 默默搬来小板凳…",
                f"· {name_} 举起了打Call棒…",
                f"· {name_} 高呼：来点猛的！",
                f"· {name_} 靠在墙角边看边笑…",
                f"· {name_} 正在做表情包素材采集…"
            ]
            funny_line = random.choice(funny_watch_lines)

            game['kanxi_name'] += funny_line + "\n"
            await update_edit_message(call, game)
        else:
            await call.answer("❌ 您已经在围观了！", show_alert=False)
    else:
        await call.answer("❌ 您已经被盯上了！", show_alert=False)

async def surrender(call, game_id):
    game = rob_games.get(game_id)
    if game is None:
        await call.answer("❌ 这个抢劫已经无效。", show_alert=True)
        return

    if call.from_user.id == int(call.data.split("_")[4]):
        target_with_link = await get_fullname_with_link(int(call.data.split("_")[4]))
        user_with_link = await get_fullname_with_link(int(call.data.split("_")[3]))
        result_text = f"{user_with_link} 不花一兵一卒拿下🏆\n{target_with_link} 居然直接给钱懦夫😭"
        result_msg = await bot.send_message(call.message.chat.id, result_text, reply_to_message_id=call.message.id)
        asyncio.create_task(deleteMessage(result_msg, 180))
        await update_edit_message(call, game, 'surrender')
    else:
        await call.answer("❌ 您只是围观群众！", show_alert=False)

async def fighting(call, game_id):
    game = rob_games.get(game_id)
    if game is None:
        await call.answer("❌ 这个抢劫已经无效。", show_alert=True)
        return

    if call.from_user.id == int(call.data.split("_")[4]):
        # 开始决斗
        if game["round_time"] < 3:
            game["round_time"] += 1
            game["user_score"] += random.randint(0, 7)
            game['target_score'] += random.randint(0, 6)

            target_with_link = await get_fullname_with_link(int(call.data.split("_")[4]))
            user_with_link = await get_fullname_with_link(int(call.data.split("_")[3]))
            await update_edit_message(call, game)
            if game["round_time"] >= 3:
                user = sql_get_emby(int(call.data.split("_")[3]))
                target_user = sql_get_emby(int(call.data.split("_")[4]))

                if game["target_score"] > game["user_score"]:
                    actual_penalty = min(user.iv, FIGHT_PENALTY)
                    message = f"{target_with_link} 以 {game['target_score']} : {game['user_score']} 击败了乱世的盗贼\n{target_with_link} 最终赢得了斗争🏆\n{user_with_link} 失去 {actual_penalty} {sakura_b}😭"
                    success_msg = await bot.send_message(call.message.chat.id, message, reply_to_message_id=call.message.id)
                    asyncio.create_task(deleteMessage(success_msg, 180))
                    
                    change_emby_amount(user.tg, user.iv - actual_penalty)
                    change_emby_amount(call.from_user.id, target_user.iv + actual_penalty)
                    
                    await bot.send_message(
                        user.tg,
                        f"😌 抢劫失败\n\n乱世的盗贼抢劫失败\n损失：{actual_penalty} {sakura_b}\n余额：{sql_get_emby(user.tg).iv} {sakura_b}",
                        reply_to_message_id=call.message.id)
                        
                    await bot.send_message(
                        target_user.tg,
                        f"🎉 逃过一杰\n\n你打赢了乱世的盗贼\n获得：{actual_penalty} {sakura_b}\n余额：{sql_get_emby(target_user.tg).iv} {sakura_b}",
                        reply_to_message_id=call.message.id)
                        
                elif game["target_score"] < game["user_score"]:
                    if target_user.iv < game['rob_gold']:
                        rob_gold = target_user.iv
                        message = f"乱世的盗贼以 {game['user_score']} : {game['target_score']} 抢劫成功\n{target_with_link} 是个穷鬼全被抢走了🤡\n{user_with_link} 穷鬼也不放过抢走 {rob_gold} {sakura_b}🏆"
                    else:
                        rob_gold = to_int(game['rob_gold'])
                        message = f"乱世的盗贼以 {game['user_score']} : {game['target_score']} 抢劫成功\n{target_with_link} 最终反抗失败🤡\n{user_with_link} 抢走 {rob_gold} {sakura_b}🏆"
                    
                    await bot.send_message(
                        user.tg,
                        f"🎉 抢劫成功\n\n乱世的盗贼以 {game['user_score']} : {game['target_score']} 抢劫成功\n获得：{rob_gold} {sakura_b}\n余额：{user.iv + rob_gold} {sakura_b}",
                        reply_to_message_id=call.message.id
                    )
                    await bot.send_message(
                        target_user.tg,
                        f"😌 防守失败\n\n你以 {game['target_score']} : {game['user_score']} 败给了乱世的盗贼\n损失：{rob_gold} {sakura_b}\n余额：{target_user.iv - rob_gold} {sakura_b}",
                        reply_to_message_id=call.message.id
                    )

                    change_emby_amount(user.tg, user.iv + rob_gold)
                    change_emby_amount(target_user.tg, target_user.iv - rob_gold)

                    rob_msg = await bot.send_message(call.message.chat.id, message, reply_to_message_id=call.message.id)
                    asyncio.create_task(deleteMessage(rob_msg, 180))
                else:
                    message = f"双方竟然打平了, 乱世的盗贼跑路了，{user_with_link} 痛失佣金 💸，什么也没有得到 🤡"
                    rob_msg = await bot.send_message(call.message.chat.id, message, reply_to_message_id=call.message.id)
                    asyncio.create_task(deleteMessage(rob_msg, 180))
                    
                    await bot.send_message(
                        user.tg,
                        f"😌 抢劫失败\n\n平手\n损失：{COMMISSION_FEE} {sakura_b}\n余额：{user.iv} {sakura_b}！",
                        reply_to_message_id=call.message.id
                    )
                    await bot.send_message(
                        target_user.tg,
                        f"🎉 逃过一杰\n\n平手，成功保住财产\n余额：{target_user.iv} {sakura_b}！",
                        reply_to_message_id=call.message.id
                    )
                
                asyncio.create_task(handle_kanxi_rewards(game))
                asyncio.create_task(deleteMessage(call.message, 180))
                del rob_games[game_id]
    else:
        await call.answer("❌ 您只是围观群众！", show_alert=False)

async def handle_kanxi_rewards(rob_game):
    kanxi_list = rob_game['kanxi_list']
    total_rewards = 0

    luck_roll = random.randint(1, 10000)

    if kanxi_list:
        reward_messages = []
        tasks = [] 

        for kanxi_id in kanxi_list:
            name = await get_fullname_with_link(kanxi_id)
            kanxi_user = sql_get_emby(kanxi_id)
            
            if luck_roll == 1:
                change_emby_amount(kanxi_id, kanxi_user.iv + LUCKY_AMOUNT)
                reward_messages.append(f". 恭喜 {name} 获得幸运大奖， 奖金 {LUCKY_AMOUNT} {sakura_b} 🥳")
            else:
                reward_chance = random.randint(1, 100)
                if reward_chance <= PENALTY_CHANCE:
                    # 惩罚
                    penalty = min(PENALTY_AMOUNT, kanxi_user.iv)
                    if penalty > 0:
                        change_emby_amount(kanxi_id, kanxi_user.iv - penalty)
                        remaining_gold = sql_get_emby(kanxi_id).iv
                        reward_messages.append(f"· {name} 被误伤，损失 {penalty} {sakura_b}🤕")
                        tasks.append(bot.send_message(kanxi_id, f"您被误伤，损失了 {penalty} {sakura_b}😭，剩余 {remaining_gold} {sakura_b}"))
                
                elif reward_chance <= PENALTY_CHANCE + BONUS_CHANCE:
                    bonus_amount = to_int(random.randint(BONUS_MIN_AMOUNT, BONUS_MAX_AMOUNT))
                    if total_rewards + bonus_amount > TOTAL_GAME_COINS * 2: 
                        bonus_amount = to_int(TOTAL_GAME_COINS / 2)
                    
                    if bonus_amount > 0:
                        change_emby_amount(kanxi_id, kanxi_user.iv + bonus_amount)
                        total_rewards += bonus_amount
                        remaining_gold = sql_get_emby(kanxi_id).iv
                        reward_messages.append(f"· {name} 捡到了 {bonus_amount} {sakura_b}，爽🥳")
                        tasks.append(bot.send_message(kanxi_id, f"您捡到了 {bonus_amount} {sakura_b}🍉，剩余 {remaining_gold} {sakura_b}"))
                else:
                    remaining_gold = sql_get_emby(kanxi_id).iv
                    reward_messages.append(f"· {name} 光顾着围观了，啥也没捞到😕")
                    tasks.append(bot.send_message(kanxi_id, f"您什么也没捞到😕，剩余 {remaining_gold} {sakura_b}"))

        if tasks:
            await asyncio.gather(*tasks)

        reward_message = "· 📺 围观群众\n" + "\n".join(reward_messages)
        result = await bot.send_message(rob_game['chat_id'], reward_message,
                                        reply_to_message_id=rob_game["original_message"].id)

        asyncio.create_task(deleteMessage(result, 180))

@bot.on_callback_query(filters.regex(r"rob_"))
async def handle_rob_callback(client, call):
    game_id = call.message.id
    lock = get_lock(game_id)

    async with lock:
        try:
            parts = call.data.split('_')
            if not sql_get_emby(call.from_user.id):
                await call.answer(f"❌ 您还未在系统中初始化，请先私信我激活", show_alert=True)
                return
            
            if not game.rob_no_emby:
                if not sql_get_emby(call.from_user.id).embyid:
                    await call.answer("❌ 您还未注册Emby账户！", show_alert=True)
                    return
            if len(parts) < 5:
                await call.answer("❌ 无效的回调数据。", show_alert=True)
                return
            if game_id not in rob_games:
                await call.answer("❌ 这个抢劫已经无效。", show_alert=True)
                return

            if parts[1] == 'kanxi':
                await onlookers(call)
            elif parts[1] == 'flee':
                await surrender(call, game_id)
            elif parts[1] == 'fight':
                await fighting(call, game_id)
        except Exception as e:
            print(f"Error handling callback: {e}")
            await call.answer("❌ 处理请求时出错。", show_alert=True)
        finally:
            pass

@bot.on_message(filters.command('rob', prefixes=prefixes) & filters.group)
async def rob_user(_, message):
    if not game.rob_open:
        try:
            await message.delete()
        except:
            pass
        return
        
    user = sql_get_emby(message.from_user.id)
    if not user:
        asyncio.create_task(deleteMessage(message, 0))
        error_msg = await bot.send_message(message.chat.id, f"❌ 您还未在系统中初始化，请先私信我激活")
        asyncio.create_task(deleteMessage(error_msg, 3))
        return

    if not message.reply_to_message:
        if len(message.command) != 2:
            asyncio.create_task(deleteMessage(message, 0))
            error_msg = await bot.send_message(message.chat.id, "❌ 请使用正确的格式：/rob [目标用户ID] 或回复某人的消息使用 /rob")
            asyncio.create_task(deleteMessage(error_msg, 3))
            return

    if not game.rob_no_emby:
        if not user.embyid:
            asyncio.create_task(deleteMessage(message, 0))
            error_msg = await bot.send_message(message.chat.id, '❌ 您还未注册Emby账户')
            asyncio.create_task(deleteMessage(error_msg, 3))
            return

    asyncio.create_task(deleteMessage(message, 0))

    target_user = sql_get_emby(message.reply_to_message.from_user.id)
    if not target_user:
        asyncio.create_task(delete_msg_with_error(message, f'❌ 目标用户未在系统中初始化，无法抢劫'))
        return

    if message.from_user.id == message.reply_to_message.from_user.id:
        asyncio.create_task(delete_msg_with_error(message, "❌ 不能抢劫自己哦"))
        return

    for item in rob_games.values():
        if item['target_user_id'] == target_user.tg:
            asyncio.create_task(delete_msg_with_error(message, '❌ 乱世的盗贼外出了，请稍后再雇佣!'))
            return

    if target_user.iv <= MIN_ROB_TARGET:
        asyncio.create_task(delete_msg_with_error(message, '❌ 对方还没凑够保护费🤡，放过他吧！'))
        return

    if user.iv < COMMISSION_FEE:
        asyncio.create_task(delete_msg_with_error(message, f'❌ 您的{sakura_b}不足以支付委托费用({COMMISSION_FEE}个)'))
        return

    change_emby_amount(user.tg, user.iv - COMMISSION_FEE)
    user_with_link = await get_fullname_with_link(user.tg)
    target_with_link = await get_fullname_with_link(target_user.tg)
    message = await bot.send_message(
        message.chat.id,
        f"接受 { user_with_link } 的委托\n委托费 {COMMISSION_FEE} 抢劫 {target_with_link}",
        reply_to_message_id=message.id
    )
    asyncio.create_task(deleteMessage(message, 30))

    await bot.send_message(
        user.tg,
        f"✅ 您已成功雇佣乱世的盗贼\n💰 扣除雇佣费：{COMMISSION_FEE} {sakura_b}\n💳 当前余额：{sql_get_emby(user.tg).iv} {sakura_b}",
        reply_to_message_id=message.id
    )
    await start_rob(message, user, target_user)

async def get_fullname_with_link(user_id):
    try:
        tg_info = await bot.get_users(user_id)
        return f"[{tg_info.first_name}](tg://user?id={tg_info.id})"
    except:
        return f"[未知用户](tg://user?id={user_id})"