"""训练与在线推理共用的网络结构；仅启用 LSTM 时导入 torch。"""
from torch import nn


class SharedLSTM(nn.Module):
    def __init__(self, hidden_size, layers, dropout, horizon):
        super().__init__()
        self.encoder = nn.LSTM(1, hidden_size, num_layers=layers, batch_first=True,
                               dropout=dropout if layers > 1 else 0.0)
        self.head = nn.Sequential(nn.Dropout(dropout), nn.Linear(hidden_size, horizon))

    def forward(self, values):
        _, (hidden, _) = self.encoder(values)
        return self.head(hidden[-1])
