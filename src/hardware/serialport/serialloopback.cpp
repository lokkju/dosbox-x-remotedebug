/*
 *  Copyright (C) 2002-2021  The DOSBox Team
 *
 *  This program is free software; you can redistribute it and/or modify
 *  it under the terms of the GNU General Public License as published by
 *  the Free Software Foundation; either version 2 of the License, or
 *  (at your option) any later version.
 *
 *  This program is distributed in the hope that it will be useful,
 *  but WITHOUT ANY WARRANTY; without even the implied warranty of
 *  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 *  GNU General Public License for more details.
 *
 *  You should have received a copy of the GNU General Public License along
 *  with this program; if not, write to the Free Software Foundation, Inc.,
 *  51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
 */


#include "dosbox.h"
#include "logging.h"
#include "pic.h"
#include "setup.h"
#include "serialport.h"
#include "serialloopback.h"

void ResolvePath(std::string& in);

CSerialLoopback::CSerialLoopback(Bitu id, CommandLine* cmd) : CSerial(id, cmd) {
	CSerial::Init_Registers();
	// DSR+CTS on so DOS COM device won't hang waiting for handshake
	setRI(false);
	setCD(false);
	setDSR(true);
	setCTS(true);

	std::string str;
	if (cmd->FindStringBegin("file:", filename, false)) {
		ResolvePath(filename);
		LOG_MSG("Serial%d: loopback will also write to %s", int(id)+1, filename.c_str());
	}

	if (cmd->FindStringBegin("timeout:", str, false)) {
		if (sscanf(str.c_str(), "%u", &timeout) != 1) {
			LOG_MSG("Serial%d: Invalid timeout parameter.", int(id)+1);
			return;
		}
	}

	LOG_MSG("Serial%d: loopback mode enabled", int(id)+1);
	InstallationSuccessful = true;
}

CSerialLoopback::~CSerialLoopback() {
	if (fp != NULL) {
		fclose(fp);
		fp = NULL;
	}
	removeEvent(SERIAL_TX_EVENT);
}

void CSerialLoopback::handleUpperEvent(uint16_t type) {
	if (fp != NULL && timeout != 0) {
		if (lastUsedTick + timeout < PIC_Ticks) {
			fclose(fp);
			fp = NULL;
			LOG_MSG("File %s for serial port closed.", filename.c_str());
		} else {
			float new_delay = (float)((timeout + 1) - (PIC_Ticks - lastUsedTick));
			setEvent(SERIAL_TX_EVENT, new_delay);
		}
	}

	if (type == SERIAL_TX_EVENT) {
		// TX complete: feed the byte back as received data, then signal done
		receiveByte(loopbackByte);
		ByteTransmitted();
	} else if (type == SERIAL_THR_EVENT) {
		ByteTransmitting();
		setEvent(SERIAL_TX_EVENT, bytetime);
	}
}

void CSerialLoopback::updatePortConfig(uint16_t divider, uint8_t lcr) {
	(void)divider;
	(void)lcr;
}

void CSerialLoopback::updateMSR() {
}

void CSerialLoopback::transmitByte(uint8_t val, bool first) {
	// Store the byte for loopback delivery on TX completion
	loopbackByte = val;

	if (first) setEvent(SERIAL_THR_EVENT, bytetime / 10);
	else setEvent(SERIAL_TX_EVENT, bytetime);

	lastUsedTick = PIC_Ticks;
	if (timeout != 0) setEvent(SERIAL_TX_EVENT, (float)(timeout + 1));

	// Optionally capture to file
	if (!filename.empty()) {
		if (fp == NULL) {
			fp = fopen(filename.c_str(), "wb");
			if (fp != NULL) setbuf(fp, NULL);
		}
		if (fp != NULL)
			fwrite(&val, 1, 1, fp);
	}
}

void CSerialLoopback::setBreak(bool value) {
	(void)value;
}

void CSerialLoopback::setRTSDTR(bool rts, bool dtr) {
	setRTS(rts);
	setDTR(dtr);
}

void CSerialLoopback::setRTS(bool val) {
	setCTS(val);
}

void CSerialLoopback::setDTR(bool val) {
	setDSR(val);
	setRI(val);
	setCD(val);
}
