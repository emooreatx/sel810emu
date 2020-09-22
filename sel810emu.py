import sys
import threading,queue
import select
import socket
import time
import cmd
import os
import json
from SELDeviceDriver import *
from ASR33OnTelnet import *
from cpserver import *
#
#TODO
#  interrupts
#  variable base register
#  BTC block transfer controller
#
#Maximum Number of BTC's per Computer  8
#Maximum Number of CGP's per Computer   6
#Maximum Number of Peripheral Devices per BTC  16
#RAM should save out to a non-volatile file since the core memory is
#join threads on exit
#
#


try:
    import readline
except ImportError:
    readline = None


sys.path.append("sel810asm")
from util import *
from MNEMBLER2 import *


CPU_HERTZ = 572000


MAX_MEM_SIZE = 0x7fff
		
OPTION_PROT_NONE 	= 0
OPTION_PROT_1B 		= 1
OPTION_PROT_2B		= 2

class RAM_CELL():
	def __init__(self,parent_array=None, prot=None, width=16):
		self.value = 0
		self.bitwidth = width
		self.prot = False
		self.parity = False
		self.parent = parent_array
		self.write_signed(0x0000)

	def read_signed(self):
		return twoscmplment2dec(self.value)

	def read(self):
		return self.value

	def write(self,v):
		overflow = False
		if v > ((2**self.bitwidth) - 1):
			overflow = True
		self.value = v & ((2**self.bitwidth) - 1)
		self.parity = parity_calc(self.value)
		return overflow

	def write_signed(self,v):
		overflow = False
		if v > ((2**self.bitwidth) - 1):
			overflow = True
			
		if self.parent != None:
			if self.parent._write_attempt(self.prot):
				self.value = dec2twoscmplment(v & ((2**self.bitwidth) - 1),self.bitwidth)
				self.parity = parity_calc(self.value)
		else: #no parent to check with
			self.value = dec2twoscmplment(v & ((2**self.bitwidth) - 1),self.bitwidth)
			self.parity = parity_calc(self.value)

		return overflow
	
class MEMORY(list):
	def __init__(self, cpu, memmax=0x7fff, options=OPTION_PROT_NONE):
		self.optionsmask = options
		self.memmax = memmax
		self.prot_reg = 0
		self._ram = [] # [RAM_CELL()] * self.memmax
		self.cpu = cpu

		if self.optionsmask & OPTION_PROT_1B: #its not clear how this option works for various memory sizes
			psize = 1024
		elif self.optionsmask & OPTION_PROT_2B:
			psize = 2048
		else:
			psize = self.memmax
			
		for i in range(0, self.memmax, psize):
			for e in range(0,psize):
				self._ram.append(RAM_CELL(self,i))
				
	def _write_attempt(self,prot_bit):
		return 1 #will figure out the interrupt logic for this later
		 
	def __setitem__(self, key, item):
		self.__dict__[key] = item

	def __getitem__(self, key):
		return self._ram[key & self.memmax]

	def set_prot_reg(self, regval):
		self.prot_reg = regval
		
	def get_prot_reg(self):
		return self.prot_reg



class SEL810CPU():
	def __init__(self,type= SEL810ATYPE):
		self.ram = MEMORY(self)
		self.type = SEL810ATYPE
		self.cpcmdqueue = queue.Queue()
		self._shutdown = False
		driver_positive_edge_tasks = []
		driver_negative_edge_tasks = []

		self.external_units = {0:ExternalUnit(self,"nulldev"),
							   1:ExternalUnit(self,"asr33",chardev=True),
							   2:ExternalUnit(self,"paper tape",chardev=True),
							   3:ExternalUnit(self,"card punch",chardev=True),
							   4:ExternalUnit(self,"card reader",chardev=True),
							   5:ExternalUnit(self,"line printer",chardev=True),
							   6:ExternalUnit(self,"TCU 1"),
							   7:ExternalUnit(self,"TCU 2"),
							   10:ExternalUnit(self,"typewriter"),
							   11:ExternalUnit(self,"X-Y plotter"),
							   12:ExternalUnit(self,"interval timer"),
							   13:ExternalUnit(self,"movable head disc"),
							   14:ExternalUnit(self,"CRT"),
							   15:ExternalUnit(self,"fixed head disc"),
				
							   32:ExternalUnit(self,"NIXIE Minute Second"),
							   33:ExternalUnit(self,"NIXIE Day Hour"),
							   34:ExternalUnit(self,"NIXIE Months"),
							   35:ExternalUnit(self,"SWITCH0"),
							   36:ExternalUnit(self,"SWITCH1"),
							   37:ExternalUnit(self,"SWITCH2"),
							   38:ExternalUnit(self,"RELAY0"),
							   39:ExternalUnit(self,"RELAY1"),
							   40:ExternalUnit(self,"SENSE0"),
							   50:ExternalUnit(self,"SENSE1"),

}
		
		
		if self.type == SEL810ATYPE:

			b_reg_cell = RAM_CELL()
			self.registers = {
				"Program Counter":RAM_CELL(width=15),
				"Instruction":RAM_CELL(),
				"Index Register":b_reg_cell,
				"A Register":RAM_CELL(),
				"B Register":b_reg_cell,
				"Protection Register":RAM_CELL(),
				"VBR Register":RAM_CELL(width=6),
				"Control Switches":RAM_CELL(),
				"Stall Counter":RAM_CELL(width=6),
			}

		elif self.type == SEL810BTYPE:
			self.registers = {
				"Program Counter":RAM_CELL(width=15),
				"Index Register":RAM_CELL(),
				"A Register":RAM_CELL(),
				"B Register":RAM_CELL(),
				"Protection Register":RAM_CELL(),
				"VBR Register":RAM_CELL(width=6),
				"Control Switches":RAM_CELL(),
			}
		self.cyclecount = 0

		self.t_register = 0
	
		self.latch = {	"halt":True,#start halted
						"iowait":False,
						"overflow":False,
						"stall":False,
						"index_pointer":False}
		
		self.load_core_memory()
		
		self.stall_ticker = 0
		self.stal_ptr = 0
		
		self.cpthread = threading.Thread(target=control_panel_backend, args=(self,))
		self.cpthread.start()

		
	def store_core_memory(self):
		coredata = []
		for i in range(MAX_MEM_SIZE):
			coredata.append(self.ram[i].read())
		storeProgramBin("sel810.coremem",coredata)

	def load_core_memory(self):
		try:
			self.loadAtAddress(0,"sel810.coremem")
		except: #if we cant load the file, no worries
			pass

	def _increment_pc(self,incrnum=1):
		self.registers["Program Counter"].write(self.registers["Program Counter"].read() + incrnum )

	def _next_pc(self):
		return (self.registers["Program Counter"].read() + 1) & MAX_MEM_SIZE

	def _increment_cycle_count(self,incrnum=1):
		for i in range(incrnum):
				self.cyclecount += 1
				if (self.cyclecount % CPU_HERTZ) == 0:
						self.fire_60_hz_interrupt()
		

	def _shift_cycle_timing(self,shifts):
		if 0 > shifts and shifts < 5:
			self._increment_cycle_count(2)
		elif 4 > shifts and shifts < 9:
			self._increment_cycle_count(3)
		elif 8 > shifts and shifts < 13:
			self._increment_cycle_count(4)
		elif 13 > shifts and shifts < 16:
			self._increment_cycle_count(5)
			
			
	def _resolve_indirect_address(self,base,m,i,x):
		#M functions only if the Indirect Flag (bit5)isa "1". Ifbit5andbit6 are both "1" bits the MSB of the program counter is merged with the Indirect Address. If bit 5 is a "1" andbit6isa "0"theMSBofthe Indirect Address is set to a "0". This feature allows the program to be executed in upper memory (MAP 40 or greater) in the same manner as it is executed in lower memory.
		
		if i:
			val =  self.ram[self.ram[(self.registers["Program Counter"].read() & 0x4000) | ((base + (x * self.registers["Index Register"].read_signed())) & MAX_MEM_SIZE)].read_signed()].read_signed()
		else:
			val =  self.ram[(base + (x * self.registers["Index Register"].read_signed())) & MAX_MEM_SIZE].read_signed()
			
		return val
		
		
	def fire_60_hz_interrupt(self):
		pass #fixme i have no idea where this fires
	
	def fire_pf_restore_interrupt(self):
		self._SPB_indir_opcode(0o1000, True)
		
	def fire_stall_interrupt(self):
		self._SPB_indir_opcode(0o1001, True)

	def fire_priority_interrupt(self,group,channel):
		self._SPB_indir_opcode(0o1002 + (group * 12) + channel, True)
		
	def _SPB_indir_opcode(self,address): #fixme
		#fixme
		address = self.ram[address].read_signed()

		self.ram[address].write_signed(self._next_pc())
		self.registers["Program Counter"].write(address)
		self._increment_cycle_count(2)
		self._increment_pc()


	def panelswitch_step_neg_edge(self):
		self.registers["Instruction"].write(self.ram[self.registers["Program Counter"].read()].read())


	def _stall_counter_task(self,cpu):
		if cpu.registers["Program Counter"].read_signed() == self.stall_ptr: #i think this whole construct assumes time ticks as peripherals wait..
			cpu.registers["Stall Counter"].write_signed(cpu.registers["Stall Counter"].read_signed() + 1)
			
		if self.stall_ticker == STALL_TICKER_COUNT:
			self.latch["stall"] = True
		
			
	def panelswitch_step_pos_edge(self):
		op  = SELOPCODE(opcode=self.registers["Instruction"].read())
		if op.nmemonic in SEL810_OPCODES:
		
			if "address" in op.fields:
#				map = op.fields["m"]  #left as a reminder
				address = op.fields["address"]
				if op.fields["i"]:
					address = self.ram[address].read_signed()
				
			if op.nmemonic == "LAA":
				self.registers["A Register"].write(self.ram[(address + (op.fields["x"] * self.registers["Index Register"].read_signed())) & MAX_MEM_SIZE].read())
				self._increment_cycle_count(2)
				self._increment_pc()

			elif op.nmemonic == "LBA":
				self.registers["B Register"].write(self.ram[(address + (op.fields["x"] * self.registers["Index Register"].read_signed())) & MAX_MEM_SIZE].read_signed())
				self._increment_cycle_count(2)
				self._increment_pc()

			elif op.nmemonic == "STA":
				self.ram[address].write(self.registers["A Register"].read())
				self._increment_cycle_count(2)
				self._increment_pc()

			elif op.nmemonic == "STB":
				self.ram[address].write(self.registers["B Register"].read())
				self._increment_cycle_count(2)
				self._increment_pc()

			elif op.nmemonic == "AMA": ##CARRY
				if (self.registers["A Register"].read_signed() + self.ram[address].read_signed()) > 0xffff:
					self.latch["overflow"] = True
				self.registers["A Register"].write (self.registers["A Register"].read_signed() + self.ram[address].read_signed())
				self._increment_cycle_count(2)
				self._increment_pc()

			elif op.nmemonic == "SMA": #CARRY
				if (self.registers["A Register"].read_signed() - self.ram[address].read_signed()) < 0:
					self.latch["overflow"] = True
				self.registers["A Register"].write_signed(self.registers["A Register"].read_signed() - self.ram[address].read_signed())
				self._increment_cycle_count(2)
				self._increment_pc()

			elif op.nmemonic == "MPY":
				if self.registers["A Register"].read_signed() * self.ram[address].read_signed() < 0:
					self.latch["overflow"] = True
				self.registers["A Register"].write_signed(self.registers["A Register"].read_signed() * self.ram[address].read_signed())
				self._increment_cycle_count(6)
				self._increment_pc()

			elif op.nmemonic == "DIV":
				if  self.ram[address].read_signed() != 0: #fixme overflow is wrong
					a = (self.registers["A Register"].read_signed() << 16 | self.registers["B Register"].read_signed()) / self.ram[address].read_signed()
					b = (self.registers["A Register"].read_signed() << 16 | self.registers["B Register"].read_signed()) % self.ram[address].read_signed()
					self.registers["A Register"].write_signed(a)
					self.registers["B Register"].write_signed(b)
				else:
					self.latch["overflow"] = True
				self._increment_cycle_count(11)
				self._increment_pc()
							
			elif op.nmemonic == "BRU":
				self.registers["Program Counter"].write(address + ( op.fields["x"] * self.registers["Index Register"].read_signed()))
				self._increment_cycle_count(2)
				
			elif op.nmemonic == "SPB":
				self.ram[address].write_signed(self._next_pc())
				self.registers["Program Counter"].write(address)
				self._increment_cycle_count(2)
				self._increment_pc()

			elif op.nmemonic == "IMS":
				t = (self.ram[address]["read"]() + 1) & 0xffff
				self.ram[address].write_signed(t)
				if t == 0:
					self._increment_pc()
				self._increment_cycle_count(3)
				self._increment_pc()

			elif op.nmemonic == "CMA":
				if self.registers["A Register"].read_signed() == self.ram[address].read_signed():
					self._increment_pc() #the next instruction is skipped.
				elif self.registers["A Register"].read_signed() > self.ram[address].read_signed():
					self._increment_pc(2) #the next two instructions are skipped.
				self._increment_cycle_count(3)
				self._increment_pc()

			elif op.nmemonic == "AMB":
				if  (self.registers["B Register"].read_signed() + self.ram[address].read_signed()) > 0x7fff:
					self.latch["overflow"] = True
				self.registers["B Register"].write_signed(self.registers["B Register"].read_signed() + self.ram[address].read_signed())
				self._increment_cycle_count(2)
				self._increment_pc()
			 			 
			elif op.nmemonic == "CEU":
				if op.fields["i"]:
					addridx = self.ram[self._next_pc()].read_signed()
					addr = addridx & 0x3fff
					i = (0x4000 & addridx) >> 14
					x = (0x8000 & addridx) >> 15
					val = self._resolve_indirect_address(address,op.fields["m"],i,x)
				else:
					val  = self.ram[self._next_pc()].read_signed()
					
				if op.fields["unit"] not in self.external_units:
					eu = self.external_units[0]
				else:
					eu = self.external_units[op.fields["unit"]]
				if eu.unit_ready("r") or op.fields["wait"]:
					eu.unit_command(val)
					self._increment_cycle_count(1)
				else:
					self._increment_pc()
				self._increment_cycle_count(4)
				self._increment_pc()

			elif op.nmemonic == "TEU":
				if op.fields["i"]:
					addridx = self.ram[self._next_pc()].read_signed()
					addr = addridx & 0x3fff
					i = (0x4000 & addridx) >> 14
					x = (0x8000 & addridx) >> 15
					val = self._resolve_indirect_address(address,op.fields["m"],i,x)
				else:
					val  = self.ram[self._next_pc()].read_signed()
					
				if op.fields["unit"] not in self.external_units:
					eu = self.external_units[0]
				else:
					eu = self.external_units[op.fields["unit"]]
					
				if eu.unit_ready("r") or op.fields["wait"]:
					eu.unit_test(val)
					self._increment_cycle_count(1)
				else:
					self._increment_pc()
				self._increment_cycle_count(4)
				self._increment_pc()

			elif op.nmemonic == "SNS":
				if self.registers["Control Switches"].read_signed() & (1 << op.fields["unit"]):
					self._increment_pc()
				else: #if switch is NOT set, the next instruction is skipped.
					self._increment_pc(2)
				self._increment_cycle_count(1)

			elif op.nmemonic == "AIP":
				if op.fields["unit"] not in self.external_units:
					eu = self.external_units[0]
				else:
					eu = self.external_units[op.fields["unit"]]

				if eu.unit_ready("r") or op.fields["wait"]:
					if not op.fields["r"]:
						self.registers["A Register"].write_signed(0)
					self._increment_cycle_count(1)
					self.registers["A Register"].write_signed(self.registers["A Register"].read_signed() + eu.unit_read())
					#wait
				else:
					self._increment_pc() #skip
				self._increment_cycle_count(4)
				self._increment_pc()

			elif op.nmemonic == "AOP":
				if op.fields["unit"] not in self.external_units:
					eu = self.external_units[0]
				else:
					eu = self.external_units[op.fields["unit"]]

				if eu.unit_ready("r") or op.fields["wait"]:
					self._increment_cycle_count(1)
					eu.unit_write(self.registers["A Register"].read_signed())
					#wait
				else:
					self._increment_pc() #skip
				self._increment_cycle_count(4)
				self._increment_pc()

			elif op.nmemonic == "MIP":
				self.accumulator_a = ord(self.external_units[op.fields["unit"]].unit_read())
				self._increment_pc()
				self._increment_cycle_count(4)

				if op.fields["unit"] not in self.external_units:
					eu = self.external_units[0]
				else:
					eu = self.external_units[op.fields["unit"]]

				if eu.unit_ready("w") or op.fields["wait"]:
#					if not op.fields["r"]:
##						self.accumulator_a = 0
#						self.registers["A Register"].write_signed(0)
					self._increment_cycle_count(1)
					self.ram[self._next_pc()].write_signed(eu.unit_read())
					#wait
				else:
					self._increment_pc() #skip
				self._increment_cycle_count(4)
				self._increment_pc()

			elif op.nmemonic == "MOP":
				if op.fields["unit"] not in self.external_units:
					eu = self.external_units[0]
				else:
					eu = self.external_units[op.fields["unit"]]

				if eu.unit_ready("w") or op.fields["wait"]:
					self._increment_cycle_count(1)
					eu.unit_write(self.ram[self._next_pc()].read_signed())
					#wait
				else:
					self._increment_pc() #skip
				self._increment_cycle_count(4)
				self._increment_pc(2)

			elif op.nmemonic == "HLT":
				self.latch["halt"] = True
				self._increment_cycle_count()

			elif op.nmemonic == "RNA":
				if self.registers["B Register"].read() & 0x4000:
					if (self.registers["A Register"].raw_read() + 1) > 0x7fff:
						self.latch["overflow"] = True
					self.registers["A Register"].write_signed(self.registers["A Register"].read_signed() + 1)
				self._increment_cycle_count(1)
				self._increment_pc()

			elif op.nmemonic == "NEG": #fixme
				self.registers["A Register"].write_signed(self.registers["B Register"].raw_read()) #twoscomplement applied on write()
				self._increment_cycle_count()
				self._increment_pc()

			elif op.nmemonic == "CLA":
				self.registers["A Register"].write_signed(0)
				self._increment_cycle_count()
				self._increment_pc()

			elif op.nmemonic == "TBA":
				self.registers["A Register"].write_signed(self.registers["B Register"].read_signed())
				self._increment_cycle_count(1)
				self._increment_pc()
				 
			elif op.nmemonic == "TAB":
				self.registers["B Register"].write_signed(self.registers["A Register"].read_signed())
				self._increment_cycle_count(1)
				self._increment_pc()

			elif op.nmemonic == "IAB":
				t = self.registers["A Register"].read_signed()
				self.registers["A Register"].write_signed(self.registers["B Register"].read_signed())
				self.registers["B Register"].write_signed(t)
				self._increment_pc()
				self._increment_cycle_count(1)
				
			elif op.nmemonic == "CSB":
				if self.registers["B Register"].read() & 0x8000:
					self.carry_flag = True
				else:
					self.carry_flag = False
				self.registers["B Register"].write_signed(self.registers["A Register"].raw_read() & 0x7fff)
				self._increment_pc()
				self._increment_cycle_count(1)
				
			elif op.nmemonic == "RSA":
				s  = self.registers["A Register"].read() & 0x8000
				for i in op.fields["shifts"]:
					self.registers["A Register"].write(s | (self.registers["A Register"].read() >> 1))
				self._shift_cycle_timing(op.fields["shifts"])
				self._increment_pc()

			elif op.nmemonic == "LSA":
				s  = self.registers["A Register"].read() & 0x8000
				self.registers["A Register"].write(s | ((self.registers["A Register"].read() << op.fields["shifts"]) & 0x7fff))
				self._shift_cycle_timing(op.fields["shifts"])
				self._increment_pc()

			elif op.nmemonic == "FRA":
				s1  = self.registers["A Register"].read() & 0x8000
				s2  = self.registers["B Register"].read() & 0x8000
				r = ((self.registers["A Register"].read() & 0x7fff) << 15) | (self.registers["B Register"].read() & 0x7fff)
				for i in op.fields["shifts"]:
					r = s1 | (r >> 1)
				self.registers["A Register"].write(s1 | ((r >> 15) & 0x7fff))
				self.registers["B Register"].write(s2 | (r & 0x7fff))
				self._shift_cycle_timing(op.fields["shifts"])
				self._increment_pc()

			elif op.nmemonic == "FLL":
				t = (((self.registers["A Register"].read() << 16) | self.registers["B Register"].read()) << op.fields["shifts"]) & 0xffffffff
				self.registers["A Register"].write((t  >> 16) & 0xffff)
				self.registers["B Register"].write((t & 0xffff))
				self._shift_cycle_timing(op.fields["shifts"])
				self._increment_pc()

			elif op.nmemonic == "FRL":
				for i in range(op.fields["shifts"]):
					t = (((self.registers["A Register"].read() << 16) | self.registers["B Register"].read()) << 1)
					b = t & (0x10000) >> 16
					t = t | b
				self.registers["A Register"].write((t  >> 16) & 0xffff)
				self.registers["B Register"].write((t & 0xffff))
				self._shift_cycle_timing(op.fields["shifts"])
				self._increment_pc()

			elif op.nmemonic == "RSL":
				self.registers["A Register"].write(self.registers["A Register"].read() >> op.fields["shifts"])
				self._shift_cycle_timing(op.fields["shifts"])
				self._increment_pc()

			elif op.nmemonic == "LSL":
				self.registers["A Register"].write(self.registers["A Register"].read() << op.fields["shifts"])
				self._shift_cycle_timing(op.fields["shifts"])
				self._increment_pc()

			elif op.nmemonic == "FLA": #fixme
				s1  = self.registers["A Register"].read() & 0x8000
				s2  = self.registers["B Register"].read() & 0x8000
				r = ((((self.registers["A Register"].read() & 0x7fff) << 15) | (self.registers["B Register"].read() & 0x7fff))  << op.fields["shifts"]) & 0xffffffff
				self.registers["A Register"].write(s1 | ((r >> 15) & 0x7fff))
				self.registers["B Register"].write(s2 | (r & 0x7fff))
				self._shift_cycle_timing(op.fields["shifts"])
				self._increment_pc()
				
			elif op.nmemonic == "ASC":
				self.registers["A Register"].write(self.registers["A Register"].read() ^ 0x8000)
				self._increment_pc()
				self._increment_cycle_count(1)
				
			elif op.nmemonic == "SAS":
				if self.registers["A Register"].read_signed() == 0:
					self._increment_pc()
				elif self.registers["A Register"].read_signed()> 0:
					self._increment_pc(2)
				self._increment_pc()
				self._increment_cycle_count(1)

			elif op.nmemonic == "SAZ":
				if self.registers["A Register"].read_signed() == 0:
					self._increment_pc()
				self._increment_cycle_count(1)
				self._increment_pc()

			elif op.nmemonic == "SAN":
				if self.registers["A Register"].read_signed() < 0:
					self._increment_pc()
				self._increment_cycle_count(1)
				self._increment_pc()

			elif op.nmemonic == "SAP":
				if self.registers["A Register"].read_signed() > 0:
					self._increment_pc()
				self._increment_cycle_count(1)
				self._increment_pc()

			elif op.nmemonic == "SOF":
				if self.latch["overflow"] == True:#If the arithmetic overflow latch is set, it is reset and the next instruction is executed;
					self.latch["overflow"] = False
				else: #if the latch is reset, the next instruction is skipped.
					self._increment_pc()
				self._increment_cycle_count(2)
				self._increment_pc()

			elif op.nmemonic == "IBS":
				self.registers["B Register"].write_signed(self.registers["B Register"].read_signed() + 1)
				if self.registers["B Register"].read_signed()  == 0:
					self._increment_pc(2)
				else:
					self._increment_pc()
				self._increment_cycle_count(1)
					
			elif op.nmemonic == "ABA":
				self.registers["A Register"].write(self.registers["A Register"].read() & self.registers["B Register"].read())
				self._increment_cycle_count(1)
				self._increment_pc()

			elif op.nmemonic == "OBA":
				self.registers["A Register"].write(self.registers["A Register"].read() | self.registers["B Register"].read())
				self._increment_cycle_count(1)
				self._increment_pc()

			elif op.nmemonic == "LCS":
				self.registers["A Register"].write(self.registers["Control Panel Switches"].read())
				self._increment_cycle_count(1)
				self._increment_pc()

			elif op.nmemonic == "SNO": #If bit Al does not equal bit AO of the A~Accurnulator, the next instruction is skipped
				if(self.registers["A Register"].read() & 0x0001) != ((self.registers["A Register"].read() * 0x0002) >> 1):
					self._increment_pc()
				self._increment_cycle_count(1)
				self._increment_pc()

			elif op.nmemonic == "NOP":
				self._increment_cycle_count(1)
				self._increment_pc()

			elif op.nmemonic == "CNS":
				self._increment_cycle_count(1)
				self._increment_pc()
				
			elif op.nmemonic == "TOI":
				self._increment_cycle_count(1)
				self._increment_pc()

			elif op.nmemonic == "LOB":
				self.registers["Program Counter"].write(self.ram[self._next_pc()])
				self._increment_cycle_count(2)
				self._increment_pc(2)
				
			elif op.nmemonic == "OVS":
				self.latch["overflow"] = True
				self._increment_cycle_count(1)
				self._increment_pc()

			elif op.nmemonic == "STX":
				indir = self.ram[self._next_pc()] & 0x4000
				idx = self.ram[self._next_pc()] & 0x8000
				addr = self.ram[self._next_pc()] & 0x3000
				
				if not indir:
					self.ram[addr] = self.registers["Index Register"].read_signed()
					self._increment_cycle_count(2)
				else:
					self.ram[self.ram[addr].read_signed()] = self.registers["Index Register"].read_signed()
					self._increment_cycle_count(3) #GUESSING
				self._increment_pc(2)

			elif op.nmemonic == "TPB":
				self.registers["B Register"].write(self.registers["Protect Register"].read())
				self._increment_cycle_count(1)
				self._increment_pc()

			elif op.nmemonic == "TBP":
				self.registers["Protect Register"].write(self.registers["B Register"].read())
				self._increment_cycle_count(1)
				self._increment_pc()

			elif op.nmemonic == "TBV":
				self.registers["VBR Register"].write(self.registers["B Register"].read())
				self._increment_cycle_count(1)
				self._increment_pc()

			elif op.nmemonic == "TVB":
				self.registers["B Register"].write(self.registers["VBR Register"].read())
				self._increment_cycle_count(1)
				self._increment_pc()

			elif op.nmemonic == "LIX": #fixme
				self._increment_cycle_count(2)
				self._increment_cycle_count(1)
				self._increment_pc()

			elif op.nmemonic == "XPX":
				self.latch["index_pointer"] = True
				self._increment_cycle_count(1)
				self._increment_pc()

			elif op.nmemonic == "XPB":
				self.latch["index_pointer"] = False
				self._increment_cycle_count(1)
				self._increment_pc()

			elif op.nmemonic == "SXB":
				self.ram[address].write_signed(self.registers["B Register"].read_signed())
				self._increment_cycle_count(2)
				self._increment_pc()

			elif op.nmemonic == "IXS":
				if self.registers["Index Register"].read_signed() + 1 > 0x7fff:
					self._increment_pc()
				self.registers["Index Register"].write_signed(self.registers["Index Register"].read_signed() + 1)
				self._increment_cycle_count(1)
				self._increment_pc()

			elif op.nmemonic == "TAX":
				self.registers["Index Register"].write_signed(self.registers["A Register"].read_signed())
				self._increment_cycle_count(1)
				self._increment_pc()

			elif op.nmemonic == "TXA":
				self.registers["A Register"].write_signed(self.registers["Index Register"].read_signed())
				self._increment_cycle_count()
				self._increment_pc()

			elif op.nmemonic == "PID":
				self._increment_pc()
				
			elif op.nmemonic == "PIE":
				self._increment_pc()
		else:
			pass
			
			
	def get_cpu_state(self):
		state_struct = {}
		for n,v in self.registers.items():
			state_struct[n] = v.read()
		for n,v in self.latch.items():
			state_struct[n] = v
		state_struct["cyclecount"] = self.cyclecount
		return state_struct


	def set_cpu_state(self,state_struct):
		for n,v in self.registers.items():
			if n in state_struct:
				self.registers[n].write(state_struct[n])
				
		for n,v in self.latch.items():
			if n in state_struct:
				self.latch[n].write_raw (state_struct[n])

	def loadAtAddress(self,address,file):
		binfile = loadProgramBin(file)
		for i in range(0,len(binfile)):
			self.ram[address+i].write(binfile[i])
			
			
	def shutdown(self):
		self._shutdown = True
		self.cpthread.join()
		for n,u in self.external_units.items():
			u._teardown()
		self.store_core_memory()



def parse_inputint(val):
	print("test",val)
	try:
		val = val.strip()
		if val[0] == "'": #octal
			return int(val[1:],8)

		elif len(val) > 2 and val[:2] == "0o": #modern octal
			return int(val,8)

		elif len(val) > 2 and val[:2] == "0x": #hex
			return int(val,16)

		elif len(val) > 2 and val[:2] == "0b": #binary
			return int(val,2)

		elif val.isnumeric(): #flat number
			return(int(val))
		else:
			return  None
	except ValueError:
		return None


EXIT_FLAG = False

def control_panel_backend(cpu):
	while cpu._shutdown == False:
		if cpu.cpcmdqueue.qsize():
			(c, data) = cpu.cpcmdqueue.get()
			if c == "s":
				for i in range(data):
					cpu.panelswitch_step_neg_edge()
					cpu.panelswitch_step_pos_edge()
					print("(next op:%s)" % SELOPCODE(opcode=cpu.ram[cpu.registers["Program Counter"].read()].read_signed()).pack_asm()[0] ) #probably not the way to do this anymore

			elif c == "u":
				cpu.set_cpu_state(data)
				
			elif c == "l":
				(addr,file) = data
				cpu.loadAtAddress(addr,file)
				
			elif c == "h+":
				cpu.latch["halt"] = True

			elif c == "h-":
				cpu.latch["halt"] = False

			elif c == "q":
				running = False
				
		if not cpu.latch["halt"]:
			cpu.panelswitch_step_neg_edge()
			cpu.panelswitch_step_pos_edge()
			
		else:
			time.sleep(.1)
		

class SEL810Shell(cmd.Cmd):
	intro = 'Welcome to the SEL emulator/debugger. Type help or ? to list commands.\n'
	prompt = '(SEL810x) '
	file = None
	cpu = SEL810CPU()
	
	exit_flag = False
	histfile = os.path.expanduser('~/.sel810_console_history')
	histfile_size = 1000

	def do_step(self, arg):
		'singlestep the processor'
		if len(arg.split(" ")) > 1:
			steps = parse_inputint(arg.split(" ")[0])
		else:
			if arg != "":
				steps = parse_inputint(arg)
			else:
				steps = 1
		
		self.cpu.cpcmdqueue.put(("s",steps))

	def do_toggle_run_stop(self, arg):
		'execute until a halt is recieved'
		self.cpu.run()

	def do_load(self, arg):
		'load a binary file into memory at an address load [address] [filename]'
		try:
			(addr,file) = arg.split(" ")
		except ValueError:
			print("not enough arguments provided")
			return False
		addr = parse_inputint(addr) # fixme, should be flexible
		self.cpu.cpcmdqueue.put(("l",(addr,file)))


	def do_setpc(self,arg):
		'set the program counter to a specific memory location'
		try:
			(progcnt,) = arg.split(" ")
		except ValueError:
			print("not enough arguments provided")
			return False
		
		progcnt = parse_inputint(progcnt) # fixme, should be flexible
		self.cpu.cpcmdqueue.put(("u",{"Program Counter":progcnt}))

	def do_quit(self,args):
		'exit the emulator'
		self.exit_flag = True
		self.cpu.cpcmdqueue.put(("q",None))
		self.cpu.shutdown()
		return True
		
	def do_hexdump(self,arg):
		'hexdump SEL memory, hexdump [offset] [length]'
		try:
			(offset,length) = arg.split(" ")
			offset = parse_inputint(offset)
			length = parse_inputint(length)
		except ValueError:
			print("not enough arguments provided")
			return False

		for i in range(offset,offset+length,8):
			print("0x%04x\t" % i,end='')
			for e in range(8):
				print("0x%04x "% self.cpu.ram[i+e].read(),end="")
			print("")

	def do_octdump(self,arg):
		'octdump SEL memory, octdump [offset] [length]'
		try:
			(offset,length) = arg.split(" ")
			offset = parse_inputint(offset)
			length = parse_inputint(length)
		except ValueError:
			print("not enough arguments provided")
			return False

		for i in range(offset,offset+length,8):
			print("0o%06o\t" % i,end='')
			for e in range(8):
				print("0o%06o "% self.cpu.ram[i+e].read(),end="")
			print("")

	def do_halt(self,arg):
		'halt running cpu'
		self.cpu.cpcmdqueue.put(("h+",None))
		
		
	def do_disassemble(self,arg):
		'disassemble SEL memory, disassemble [offset] [length]'
		try:
			(offset,length) = arg.split(" ")
			offset = parse_inputint(offset)
			length = parse_inputint(length)
		except ValueError:
			print("not enough arguments provided")
			return False

		for i in range(offset,offset+length):
			op = SELOPCODE(opcode=self.cpu.ram[i].read())
			print("'%06o" % i, op.pack_asm()[0])
			

	def do_registers(self,args):
		'show the current register contents'
		for n,v in self.cpu.get_cpu_state().items():
			if n in self.cpu.registers:
				print("%s0x%04x ('%06o)" % (("%s:"%n).ljust(25) ,v,v))
			elif n in self.cpu.latch:
				print("%s%s" % (("%s:"%n).ljust(25) ,str(v)))

	def postcmd(self,arg,b):
		return arg

	def preloop(self):
		if readline and os.path.exists(self.histfile):
			readline.read_history_file(self.histfile)

	def postloop(self):
		if readline:
			readline.set_history_length(self.histfile_size)
			readline.write_history_file(self.histfile)

#	def precmd(self, line):
#		pass

	def close(self):
		pass
		
		
		




		
if __name__ == '__main__':


	shell = SEL810Shell()
	
	telnet = ASR33OnTelnetDriver(shell.cpu, "/tmp/SEL810_asr33","0.0.0.0",9999)
	cp  = ControlPanelDriver(shell.cpu,"/tmp/SEL810_control_panel")
	cp.start()
	telnet.start()
	shell.cmdloop()

	telnet.stop()
	cp.stop()
