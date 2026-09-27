import asyncio
import random
from datetime import datetime, timedelta
from typing import Dict, List
from pyrogram import filters
from bot import bot, prefixes, sakura_b, game, LOGGER
from bot.func_helper.msg_utils import deleteMessage
from bot.sql_helper.sql_emby import sql_get_emby, sql_update_emby, Emby

async def get_fullname_with_link(user_id):
    try:
        tg_info = await bot.get_users(user_id)
        name = (tg_info.first_name or f"用户{user_id}").replace("[", "").replace("]", "")
        return f"[{name}](tg://user?id={tg_info.id})"
    except Exception:
        return f"用户{user_id}"

# 存储活跃赌局的字典 (chat_id -> bet_info)
active_bets: Dict[int, Dict] = {}
# 存储参与者信息 (bet_id -> list of participants)
bet_participants: Dict[str, List[Dict]] = {}

class BettingSystem:
    def __init__(self):
        self.active_bets = active_bets
        self.participants = bet_participants
        self.lock = asyncio.Lock()
    
    def set_start_message_id(self, chat_id: int, message_id: int):
        if chat_id in self.active_bets:
            self.active_bets[chat_id]['start_message_id'] = message_id

    async def start_bet(self, chat_id: int, user_id: int, message_text: str = "") -> str:
        """创建新的赌局"""
        async with self.lock:
            # 检查是否已有进行中的赌局
            if chat_id in self.active_bets:
                return "🚫 当前已有进行中的赌局，请等待结束后再开始新的赌局"
            
            # 解析随机方式
            random_type = 'system'
            if 'dice' in message_text.lower():
                random_type = 'dice'
            
            # 创建赌局ID
            bet_id = f"{chat_id}_{int(datetime.now().timestamp())}"
            
            # 创建新赌局
            bet_info = {
                'id': bet_id,
                'chat_id': chat_id,
                'creator_id': user_id,
                'status': 1,
                'random_type': random_type,
                'create_time': datetime.now(),
                'end_time': datetime.now() + timedelta(minutes=5),
                'total_amount': 0,
                'big_amount': 0,
                'small_amount': 0,
                'start_message_id': None
            }
            
            self.active_bets[chat_id] = bet_info
            self.participants[bet_id] = []

            asyncio.create_task(self._auto_draw(chat_id, bet_id))

        user_link = await get_fullname_with_link(user_id)

        random_method = 'Telegram骰子' if random_type == 'dice' else '系统随机'
        
        return f"""🎲 新的赌局已开始！

发起者：{user_link}
手续费：{game.magnification} {sakura_b}
随机方式：{random_method}
开奖时间：{bet_info['end_time'].strftime('%H:%M:%S')}

规则说明：
1️⃣2️⃣3️⃣ 为小
4️⃣5️⃣6️⃣ 为大
数字由系统随机抽取

参与方式：
发送 /bet 大/小 金额
例如：/bet 小 10

赔率说明：奖池为总投注额的95%，按赢家投注比例分配"""
    
    async def place_bet(self, chat_id: int, user_id: int, bet_type: str, amount: str) -> str:
        """参与赌局"""
        async with self.lock:
            return await self._place_bet_impl(chat_id, user_id, bet_type, amount)

    async def _place_bet_impl(self, chat_id: int, user_id: int, bet_type: str, amount: str) -> str:
        # 验证金额
        try:
            amount_int = int(amount)
            if amount_int <= 0:
                return "❌ 请输入有效的投注金额"
            if amount_int < 1:
                return "❌ 最低投注金额为1"
        except ValueError:
            return "❌ 请输入有效的整数金额"
        
        # 验证投注类型
        if bet_type not in ['大', '小']:
            return "❌ 请选择正确的投注类型（大/小）"
        
        # 检查是否有活跃赌局
        if chat_id not in self.active_bets:
            return "❌ 当前没有进行中的赌局"
        
        bet_info = self.active_bets[chat_id]
        bet_id = bet_info['id']
        
        # 检查赌局是否已结束
        if datetime.now() > bet_info['end_time']:
            return "❌ 赌局已结束，无法继续投注"
        
        # 获取用户信息
        user = sql_get_emby(user_id)
        if not user:
            return f"❌ 您还未在系统中初始化，请先私信我激活"

        if not game.bet_no_emby:
            if not user.embyid:
                return "❌ 您还未注册Emby账户"
        
        # 检查余额
        if user.iv < amount_int:
            return "❌ 余额不足"
        
        # 检查是否已经参与
        existing_participant = None
        for participant in self.participants[bet_id]:
            if participant['user_id'] == user_id:
                existing_participant = participant
                break
        
        if existing_participant:
            # 已参与，检查是否可以追加投注
            if existing_participant['type'] != bet_type:
                return f"❌ 您已经投注了{existing_participant['type']}，不能追加投注{bet_type}"
            
            # 追加投注
            try:
                # 扣除余额
                new_balance = user.iv - amount_int
                sql_update_emby(Emby.tg == user_id, iv=new_balance)

                await bot.send_message(
                    chat_id=user_id,
                    text=f"✅ 您已成功追加赌局\n💰 追加金额：{amount_int} {sakura_b}\n💳 当前余额：{new_balance} {sakura_b}"
                )
                
                # 更新参与记录
                existing_participant['amount'] += amount_int
                
                # 更新赌局统计
                bet_info['total_amount'] += amount_int
                if bet_type == '大':
                    bet_info['big_amount'] += amount_int
                else:
                    bet_info['small_amount'] += amount_int
                
                # 计算当前赔率
                odds_info = self._calculate_odds(bet_info)
                
                user_link = await get_fullname_with_link(user_id)
                return f"""✅ {user_link} 追加投注成功！

投注类型：{bet_type}
追加金额：{amount_int} {sakura_b}
总投注额：{int(existing_participant["amount"])} {sakura_b}
开奖时间：{bet_info["end_time"].strftime("%H:%M:%S")}
当前赔率：
大：{odds_info['big_odds']:.2f}倍
小：{odds_info['small_odds']:.2f}倍
总投注：{int(bet_info['total_amount'])} {sakura_b}"""
                
            except Exception as e:
                LOGGER.info(f"用户 {user_id} 投注失败，原因: 追加投注时发生异常 - {str(e)}")
                return "❌ 追加投注失败，请稍后重试"
        
        else:
            # 首次投注
            try:
                # 扣除余额
                new_balance = user.iv - amount_int
                sql_update_emby(Emby.tg == user_id, iv=new_balance)
                
                await bot.send_message(
                    chat_id=user_id,
                    text=f"✅ 您已成功参与赌局\n💰 投注金额：{amount_int} {sakura_b}\n💳 当前余额：{new_balance} {sakura_b}"
                )
                
                # 添加参与记录
                participant = {
                    'user_id': user_id,
                    'tg_id': user.tg,
                    'type': bet_type,
                    'amount': amount_int,
                    'status': 0
                }
                self.participants[bet_id].append(participant)
                
                # 更新赌局统计
                bet_info['total_amount'] += amount_int
                if bet_type == '大':
                    bet_info['big_amount'] += amount_int
                else:
                    bet_info['small_amount'] += amount_int
                
                # 计算当前赔率
                odds_info = self._calculate_odds(bet_info)
                
                user_link = await get_fullname_with_link(user_id)
                return f"""✅ {user_link} 投注成功！

投注类型：{bet_type}
投注金额：{amount_int} {sakura_b}
开奖时间：{bet_info["end_time"].strftime("%H:%M:%S")}
当前赔率：
大：{odds_info['big_odds']:.2f}倍
小：{odds_info['small_odds']:.2f}倍
总投注：{int(bet_info['total_amount'])} {sakura_b}"""
                
            except Exception as e:
                LOGGER.info(f"用户 {user_id} 投注失败，原因: {str(e)}")
                return "❌ 投注失败，请稍后重试"
    
    def _calculate_odds(self, bet_info: Dict) -> Dict:
        """计算赔率"""
        total_amount = bet_info['total_amount']
        big_amount = bet_info['big_amount']
        small_amount = bet_info['small_amount']
        
        prize_pool = total_amount * 0.95
        
        big_odds = prize_pool / big_amount if big_amount > 0 else 0
        small_odds = prize_pool / small_amount if small_amount > 0 else 0
        
        return {
            'big_odds': big_odds if big_odds > 0 else float('inf'),
            'small_odds': small_odds if small_odds > 0 else float('inf'),
            'prize_pool': prize_pool
        }
    
    async def _auto_draw(self, chat_id: int, bet_id: str):
        """自动开奖"""
        await asyncio.sleep(300)  # 等待5分钟
        
        if chat_id not in self.active_bets:
            return
        
        bet_info = self.active_bets[chat_id]
        if bet_info['id'] != bet_id or bet_info['status'] != 1:
            return
        
        await self._draw_bet(chat_id)
    
    async def _draw_bet(self, chat_id: int) -> str:
        """执行开奖"""
        if chat_id not in self.active_bets:
            return "❌ 没有找到活跃的赌局"
        
        bet_info = self.active_bets[chat_id]
        bet_id = bet_info['id']
        
        if bet_info['status'] != 1:
            return "❌ 赌局已经结束"
        
        # 生成随机数
        if bet_info['random_type'] == 'dice':
            # 模拟Telegram骰子
            result = random.randint(1, 6)
        else:
            # 系统随机
            result = random.randint(1, 6)
        
        # 判断大小
        winning_type = '大' if result >= 4 else '小'
        
        # 计算奖励
        participants = self.participants.get(bet_id, [])
        winners = [p for p in participants if p['type'] == winning_type]
        
        odds_info = self._calculate_odds(bet_info)
        prize_pool = odds_info['prize_pool']
        
        # 分配奖励
        total_winner_amount = sum(p['amount'] for p in winners)
        
        result_message = f"""🎲 赌局开奖结果：{result} ({winning_type})

"""
        
        if winners and total_winner_amount > 0:
            for winner in winners:
                # 计算个人奖励
                personal_reward = round((winner['amount'] / total_winner_amount) * prize_pool)
                
                # 更新用户余额
                user = sql_get_emby(winner['user_id'])
                if user:
                    new_balance = user.iv + personal_reward
                    sql_update_emby(Emby.tg == winner['user_id'], iv=new_balance)
                
                winner['status'] = 1
                
                user_link = await get_fullname_with_link(winner['tg_id'])
                result_message += f"🏆 {user_link} 获得 {personal_reward} {sakura_b}\n"
        else:
            result_message += "😅 没有获胜者，投注金额不予退还\n"
        
        start_msg_id = bet_info.get('start_message_id')
        if start_msg_id:
            try:
                await bot.delete_messages(chat_id, start_msg_id)
            except Exception as e:
                LOGGER.info(f"删除赌局主消息失败: {e}")

        # 标记赌局结束
        bet_info['status'] = 0
        
        # 清理数据
        del self.active_bets[chat_id]
        if bet_id in self.participants:
            del self.participants[bet_id]

        # 发送开奖消息
        try:
            result_msg_obj = await bot.send_message(chat_id, result_message)
            asyncio.create_task(deleteMessage(result_msg_obj, 180))
        except:
            pass
            
        # 给参与者发送私信通知
        for participant in participants:
            try:
                user = sql_get_emby(participant['user_id'])
                if user:
                    won = participant['type'] == winning_type
                    if won:
                        personal_reward = round((participant['amount'] / total_winner_amount) * prize_pool) if total_winner_amount > 0 else 0
                        new_balance = user.iv
                        await bot.send_message(
                            chat_id=participant['user_id'],
                            text=f"🎉 赌局开奖通知\n\n"
                                 f"恭喜中奖！\n"
                                 f"获得：{personal_reward} {sakura_b}\n"
                                 f"当前余额：{new_balance} {sakura_b}"
                        )
                    else:
                        new_balance = user.iv
                        if not winners:
                            await bot.send_message(
                                chat_id=participant['user_id'],
                                text=f"😌 赌局开奖通知\n\n"
                                     f"本次无人中奖\n"
                                     f"投注金额不予退还\n"
                                     f"当前余额：{new_balance} {sakura_b}"
                            )
                        else:
                            await bot.send_message(
                                chat_id=participant['user_id'],
                                text=f"😔 赌局开奖通知\n\n"
                                     f"很遗憾，这次没有中奖\n"
                                     f"损失：{participant['amount']} {sakura_b}\n"
                                     f"当前余额：{new_balance} {sakura_b}"
                            )
            except Exception as e:
                LOGGER.info(f"Failed to send bet result notification: {e}")
        
        return result_message

# 创建赌局系统实例
betting_system = BettingSystem()

# 注册命令处理器
@bot.on_message(filters.command('startbet', prefixes=prefixes) & filters.group)
# 定义一个异步函数，用于处理开始下注的命令
async def handle_startbet_command(client, message):
    if not game.bet_open:
        try:
            await message.delete()
        except:
            pass
        return
    asyncio.create_task(deleteMessage(message, 0))
    chat_id = message.chat.id
    user_id = message.from_user.id
    message_text = message.text

    user = sql_get_emby(user_id)
    if not user:
        error_message = await message.reply_text(f"❌ 您还未在系统中初始化，请先私信我激活")
        asyncio.create_task(deleteMessage(error_message, 60))
        return

    if not game.bet_no_emby:
        if not user.embyid:
            error_message = await message.reply_text("❌ 您还未注册Emby账户，无法发起赌局")
            asyncio.create_task(deleteMessage(error_message, 60))
            return

    # 检查用户金币是否足够支付手续费
    if user.iv < game.magnification:
        error_message = await message.reply_text(f"❌ 你的余额不够支付 {game.magnification} {sakura_b} 手续费哦～")
        asyncio.create_task(deleteMessage(error_message, 60))
        return

    # 扣除手续费
    new_balance = user.iv - game.magnification
    sql_update_emby(Emby.tg == user_id, iv=new_balance)

    await bot.send_message(
        chat_id=user_id,
        text=f"✅ 您已成功创建赌局\n💰 扣除手续费：{game.magnification} {sakura_b}\n💳 当前余额：{new_balance} {sakura_b}"
    )

    result = await betting_system.start_bet(chat_id, user_id, message_text)
    bet_start_message = await message.reply_text(result)
    
    betting_system.set_start_message_id(chat_id, bet_start_message.id)

@bot.on_message(filters.command('bet', prefixes=prefixes) & filters.group)
async def handle_bet_command(client, message):
    if not game.bet_open:
        try:
            await message.delete()
        except:
            pass
        return
    asyncio.create_task(deleteMessage(message, 0))
    try:
        # 解析命令参数: /bet 大/小 金额
        parts = message.text.split()
        if len(parts) < 3:
            bet_reply_message = await message.reply_text("❌ 格式错误！请使用：/bet 大/小 金额")
            asyncio.create_task(deleteMessage(bet_reply_message, 60))
            return
        
        bet_type = parts[1]
        amount = parts[2]
        
        chat_id = message.chat.id
        user_id = message.from_user.id
        
        result = await betting_system.place_bet(chat_id, user_id, bet_type, amount)
        bet_reply_message = await message.reply_text(result)
        asyncio.create_task(deleteMessage(bet_reply_message, 60))        
    except Exception as e:
        error_message = await message.reply_text("❌ 命令处理失败，请检查格式")
        asyncio.create_task(deleteMessage(error_message, 60))