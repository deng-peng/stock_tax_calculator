from futu import *
import pandas as pd
from datetime import datetime, timedelta
import time
from collections import deque
from threading import Lock

class RateLimiter:
    def __init__(self, max_requests, time_window):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = deque()
        self.lock = Lock()
    
    def wait_if_needed(self):
        with self.lock:
            now = time.time()
            # 移除过期的请求记录
            while self.requests and now - self.requests[0] > self.time_window:
                self.requests.popleft()
            
            # 如果达到最大请求数，等待
            if len(self.requests) >= self.max_requests:
                wait_time = self.requests[0] + self.time_window - now
                if wait_time > 0:
                    time.sleep(wait_time)
            
            # 添加新的请求记录
            self.requests.append(time.time())

def get_history_orders():
    # 创建OpenD连接
    quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    # 不指定市场，获取所有市场的交易权限
    trade_ctx = OpenSecTradeContext(host='127.0.0.1', port=11111,filter_trdmarket=TrdMarket.NONE)
    
    # 创建请求限制器（30秒内最多10次请求）
    rate_limiter = RateLimiter(max_requests=10, time_window=50)
    
    # 存储所有账户的所有订单
    all_accounts_orders = []
    
    # 定义要查询的市场列表
    # markets_to_query = [TrdMarket.US, TrdMarket.HK]
    markets_to_query = [TrdMarket.NONE]
    
    try:
        # 获取账户列表
        ret, acc_list_df = trade_ctx.get_acc_list()
        if ret != RET_OK or not isinstance(acc_list_df, pd.DataFrame):
            print(f'获取账户列表失败: {acc_list_df}')
            return
        
        # 遍历所有账户
        for _, acc_row in acc_list_df.iterrows():
            acc_id = acc_row.get('acc_id')
            if acc_row.get("trd_env")==TrdEnv.SIMULATE:
                continue
            if acc_id is None:
                continue

            print(acc_row.get("uni_card_num"))
            
            try:
                acc_id = int(acc_id)
            except (ValueError, TypeError):
                print(f"无效的账户ID: {acc_id}")
                continue

            print(f"\n开始处理账户: {acc_id}")
            
            # 遍历所有市场
            for market in markets_to_query:
                print(f"  ...正在查询市场: {market}")
                
                # 设置查询时间范围
                start_date = datetime(2022, 1, 1)
                end_date = datetime(2024, 12, 30)
                
                # 每3个月为一个批次
                current_start = start_date
                
                while current_start < end_date:
                    # 计算当前批次的结束时间
                    current_end = min(current_start + timedelta(days=90), end_date)
                    
                    print(f"    正在获取 {current_start.strftime('%Y-%m-%d')} 到 {current_end.strftime('%Y-%m-%d')} 的订单数据...")
                    
                    # 等待请求限制
                    rate_limiter.wait_if_needed()
                    
                    # 查询历史订单, 明确指定市场
                    ret, data = trade_ctx.history_order_list_query(
                        acc_id=acc_id,
                        order_market=market,
                        start=current_start.strftime('%Y-%m-%d %H:%M:%S'),
                        end=current_end.strftime('%Y-%m-%d %H:%M:%S'),
                        status_filter_list=[OrderStatus.FILLED_ALL],
                    )
                    
                    if ret != RET_OK:
                        print(f'    获取历史订单失败: {data}')
                    
                    if isinstance(data, pd.DataFrame) and not data.empty:
                        data['acc_id'] = acc_id  # 新增：为每个订单加上acc_id
                        all_accounts_orders.append(data)
                        print(f"    成功获取 {len(data)} 条订单记录")
                    elif data is not None:
                        # 如果不是DataFrame但有内容，尝试转为DataFrame并追加acc_id
                        try:
                            data_df = pd.DataFrame(data)
                            if not data_df.empty:
                                data_df['acc_id'] = acc_id
                                all_accounts_orders.append(data_df)
                                print(f"    成功获取 {len(data_df)} 条订单记录 (非DataFrame原始类型)")
                        except Exception as e:
                            print(f"    数据无法转为DataFrame: {e}")
                    
                    # 更新下一批次的开始时间
                    current_start = current_end

        if not all_accounts_orders:
            print("所有账户和市场都未找到任何订单记录")
            return

        # 合并所有账户和市场的数据到一个DataFrame
        final_df = pd.concat(all_accounts_orders, ignore_index=True)

        # 按时间排序
        if 'create_time' in final_df.columns:
            final_df = final_df.sort_values(by='create_time', ascending=False, kind='stable')

        # ====== 新增：批量获取订单费用 ======
        if 'order_id' in final_df.columns and 'acc_id' in final_df.columns:
            fee_list = []
            batch_size = 400
            # 按账户分组批量查费用
            for acc_id_val, group in final_df.groupby('acc_id'):
                # 只处理int或str类型的acc_id
                if not isinstance(acc_id_val, (int, str)):
                    print(f'不支持的acc_id类型: {type(acc_id_val)}, 跳过该分组')
                    continue
                try:
                    acc_id_int = int(str(acc_id_val))
                except Exception:
                    print(f'无法转换acc_id: {acc_id_val}，跳过该分组')
                    continue
                order_ids = group['order_id'].tolist()
                for i in range(0, len(order_ids), batch_size):
                    batch_ids = order_ids[i:i+batch_size]
                    ret, fee_df = trade_ctx.order_fee_query(order_id_list=batch_ids, acc_id=acc_id_int, trd_env=TrdEnv.REAL)
                    if ret == RET_OK and isinstance(fee_df, pd.DataFrame):
                        fee_list.append(fee_df[['order_id', 'fee_amount']])
                    else:
                        print(f'acc_id={acc_id_int} 获取订单费用失败:', fee_df)
            if fee_list:
                all_fee_df = pd.concat(fee_list, ignore_index=True)
            else:
                all_fee_df = pd.DataFrame(columns=['order_id', 'fee_amount'])
            # 合并费用到订单表
            final_df = final_df.merge(all_fee_df, on='order_id', how='left')
            final_df.rename(columns={'fee_amount': '合计手续费'}, inplace=True)
        else:
            final_df['合计手续费'] = 0
        # ====== 新增结束 ======
        
        # 打印最终结果的汇总信息
        print(final_df)
        
        # 保存结果到统一的CSV文件
        filename = 'data/futu_history_raw.csv'
        final_df.to_csv(filename, index=False, encoding='utf-8-sig')
        print(f"\n所有账户数据已合并保存到 {filename}")
                
    finally:
        # 关闭连接
        quote_ctx.close()
        trade_ctx.close()

if __name__ == '__main__':
    get_history_orders() 