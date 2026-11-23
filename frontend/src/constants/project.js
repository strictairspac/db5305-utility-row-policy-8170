export const topic = {
  "name": "埠灯公服·抄表工单分域",
  "direction": "关系授权与行级访问策略",
  "modules": [
    "区域委派",
    "抄表工单",
    "权限验签",
    "差异清单"
  ],
  "algorithm": "构造行级访问矩阵，在 PostgreSQL RLS 中以受限角色核对可见集"
};
