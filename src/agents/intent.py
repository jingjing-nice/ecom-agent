"""统一入口的规则基线；所有分支仍执行服务端角色和数据范围检查。"""

def analytics_question(message):
    return any(word in message for word in ('运营分析', '销量', '销售额', '支付金额', '客单价', '销量排名', '库存统计', '统计', '数据分析', '退款金额', '支付件数'))


def order_question(message):
    return any(word in message for word in ('订单', '发货', '物流', '签收')) and not any(
        word in message for word in ('退款', '退货', '换货', '售后', '破损'))
