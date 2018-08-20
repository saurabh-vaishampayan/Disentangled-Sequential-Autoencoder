import os
import torch
import torch.utils.data
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
class Sprites(torch.utils.data.Dataset):
    def __init__(self,path):
        self.path = path
        self.length = 7560

    def __len__(self):
        return self.length
        
    def __getitem__(self,idx):
        return torch.load(self.path+'/%d.sprite' % idx)

class FullQDisentangledVAE(nn.Module):
    def __init__(self,frames,f_dim,z_dim,conv_dim,hidden_dim):
        super(FullQDisentangledVAE,self).__init__()
        self.f_dim = f_dim
        self.z_dim = z_dim
        self.frames = frames
        self.conv_dim = conv_dim
        self.hidden_dim = hidden_dim

        
        self.f_lstm = nn.LSTM(self.conv_dim, self.hidden_dim, 1,
                bidirectional=True,batch_first=True)
        self.f_mean = nn.Linear(self.hidden_dim*2, self.f_dim)
        self.f_logvar = nn.Linear(self.hidden_dim*2, self.f_dim)

        self.z_lstm = nn.LSTM(self.conv_dim+self.f_dim, self.hidden_dim, 1,
                 bidirectional=True,batch_first=True)
        self.z_rnn = nn.RNN(self.hidden_dim*2, self.hidden_dim,batch_first=True) 
        self.z_mean = nn.Linear(self.hidden_dim, self.z_dim)
        self.z_logvar = nn.Linear(self.hidden_dim, self.z_dim)
        
        self.conv1 = nn.Conv2d(4,256,kernel_size=3,stride=2,padding=1)
        self.bn1 = nn.BatchNorm2d(256)
        self.conv2 = nn.Conv2d(256,256,kernel_size=3,stride=2,padding=1)
        self.bn2 = nn.BatchNorm2d(256)
        self.conv3 = nn.Conv2d(256,256,kernel_size=3,stride=2,padding=1)
        self.bn3 = nn.BatchNorm2d(256)
        self.conv4 = nn.Conv2d(256,256,kernel_size=3,stride=2,padding=1)
        self.bn4 = nn.BatchNorm2d(256)
        self.conv_fc = nn.Linear(4*4*256,self.conv_dim)
        self.bnf = nn.BatchNorm1d(self.conv_dim) 

        self.deconv_fc = nn.Linear(self.f_dim+self.z_dim,4*4*256)
        self.deconv_bnf = nn.BatchNorm1d(4*4*256)
        self.deconv4 = nn.ConvTranspose2d(256,256,kernel_size=3,stride=2,padding=1,output_padding=1)
        self.dbn4 = nn.BatchNorm2d(256)
        self.deconv3 = nn.ConvTranspose2d(256,256,kernel_size=3,stride=2,padding=1,output_padding=1)
        self.dbn3 = nn.BatchNorm2d(256)
        self.deconv2 = nn.ConvTranspose2d(256,256,kernel_size=3,stride=2,padding=1,output_padding=1)
        self.dbn2 = nn.BatchNorm2d(256)
        self.deconv1 = nn.ConvTranspose2d(256,4,kernel_size=3,stride=2,padding=1,output_padding=1)
        self.dbn1 = nn.BatchNorm2d(4)
    
    def encode_frames(self,x):
        x = x.view(-1,4,64,64) #Batchwise stack the 8 images for applying convolutions parallely
        x = F.relu(self.bn1(self.conv1(x)))
        x = F.relu(self.bn2(self.conv2(x)))
        x = F.relu(self.bn3(self.conv3(x)))
        x = F.relu(self.bn4(self.conv4(x)))
        x = x.view(-1,4*4*256)
        x = F.relu(self.bnf(self.conv_fc(x))) 
        return x.view(-1,self.frames,self.conv_dim) #Convert the stack batches back into frames

    def decode_frames(self,zf):
        x = zf.view(-1,256*4*4) #For batchnorm1D to work, the frames should be stacked batchwise
        x = F.relu(self.deconv_bnf(self.deconv_fc(x)))
        x = x.view(-1,256,4,4) #The 8 frames are stacked batchwise
        x = F.relu(self.dbn4(self.deconv4(x)))
        x = F.relu(self.dbn3(self.deconv3(x)))
        x = F.relu(self.dbn2(self.deconv2(x)))
        x = F.relu(self.dbn1(self.deconv1(x))) #If images are in 0,1 range use ReLU otherwise use tanh
        return x.view(-1,self.frames,4,64,64) #Convert the stacked batches back into frames

    def reparameterize(self,mean,logvar):
        if self.training:
            eps = torch.randn_like(logvar)
            std = torch.exp(0.5*logvar)
            z = mean + eps*std
            return z
        else:
            return mean

    def encode_f(self,x):
        lstm_out,_ = self.f_lstm(x)
        mean = self.f_mean(lstm_out[:,self.frames-1]) #The forward and the reverse are already concatenated
        logvar = self.f_logvar(lstm_out[:,self.frames-1]) # TODO: Check if its the correct forward and reverse
        return mean,logvar,self.reparameterize(mean,logvar)
    
    def encode_z(self,x,f):
        f_expand = f.unsqueeze(1).expand(-1,self.frames,self.f_dim)
        lstm_out,_ = self.z_lstm(torch.cat((x, f_expand), dim=2))
        rnn_out,_ = self.z_rnn(lstm_out)
        mean = self.z_mean(rnn_out)
        logvar = self.z_logvar(rnn_out)
        return mean,logvar,self.reparameterize(mean,logvar)

    def forward(self,x):
        conv_x = self.encode_frames(x)
        f_mean,f_logvar,f = self.encode_f(conv_x)
        z_mean,z_logvar,z = self.encode_z(conv_x,f)
        f_expand = f.unsqueeze(1).expand(-1,self.frames,self.f_dim)
        recon_x = self.decode_frames(torch.cat((z,f_expand),dim=2))
        return f_mean,f_logvar,f,z_mean,z_logvar,z,recon_x

def loss_fn(original_seq,recon_seq,f_mean,f_logvar,z_mean,z_logvar):
    mse = F.mse_loss(recon_seq,original_seq,size_average=False);
    kld_f = -0.5 * torch.sum(1 + f_logvar - torch.pow(f_mean,2) - torch.exp(f_logvar))
    kld_z = -0.5 * torch.sum(1 + z_logvar - torch.pow(z_mean,2) - torch.exp(z_logvar))
    return mse + kld_f + kld_z
  

class Trainer(object):
    def __init__(self,model,device,trainloader,testloader,epochs,batch_size,learning_rate,checkpoints):
        self.trainloader = trainloader
        self.testloader = testloader
        self.start_epoch = 0
        self.epochs = epochs
        self.batch_size = batch_size
        self.model = model
        self.model.to(device)
        self.learning_rate = learning_rate
        self.checkpoints = checkpoints
        self.optimizer = optim.Adam(self.model.parameters(),self.learning_rate)
        
    def save_checkpoint(self,epoch):
        torch.save({
            'epoch' : epoch+1,
            'state_dict' : self.model.state_dict(),
            'optimizer' : self.optimizer.state_dict()},
            self.checkpoints)
        
    def load_checkpoint(self):
        try:
            print("Loading Checkpoint from '{}'".format(self.checkpoints))
            checkpoint = torch.load(self.checkpoints)
            self.start_epoch = checkpoint['epoch']
            self.model.load_state_dict(checkpoint['state_dict'])
            self.optimizer.load_state_dict(checkpoint['optimizer'])
            print("Resuming Training From Epoch {}".format(self.start_epoch))
        except:
            print("No Checkpoint Exists At '{}'.Start Fresh Training".format(self.checkpoints))
            self.start_epoch = 0

    def train(self):
       self.model.train()
       for epoch in range(self.start_epoch,self.epochs):
           losses = []
           i=0
           print("Running Epoch : {}".format(epoch))
           for data in self.trainloader:
               i += 1
               data = data.to(device)
               self.optimizer.zero_grad()
               #this part is VAE specific
               f_mean,f_logvar,f,z_mean,z_logvar,z = self.model(data)
               loss = loss_fn(data,recon_x,f_mean,f_logvar,z_mean,z_logvar)
               loss.backward()
               self.optimizer.step()
               losses.append(loss.item())
           print(len(losses) == self.batch_size)
           print("Epoch {} : Average Loss: {}".format(epoch,np.mean(losses)))
           self.save_checkpoint(epoch) 
       print("Training is complete")

vae = FullQDisentangledVAE(frames=8,f_dim=256,z_dim=32,hidden_dim=512,conv_dim=1024) #Adjust conv_dim on your own
sprites_train = Sprites('./indexed-sprites/lpc-dataset')
sprites_test = Sprites('./indexed-sprites/lpc-dataset')
trainloader = torch.utils.data.DataLoader(sprites_train,batch_size=64,shuffle=True,num_workers=4)
testloader = torch.utils.data.DataLoader(sprites_test,batch_size=64,shuffle=True,num_workers=4)
device = torch.device('cuda:0')
trainer = Trainer(vae,device,trainloader,testloader,epochs=50,batch_size=64,learning_rate=0.01,checkpoints='disentangled-vae.model') #Adjust the learning rate on your own
trainer.train()
